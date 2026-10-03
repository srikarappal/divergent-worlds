import pytest

from rlworld.specs import (
    SpecValidationError,
    dump_spec_document,
    load_spec_document,
    parse_spec_document,
    path_exists,
    set_path,
)


def rejection_text(payload) -> str:
    with pytest.raises(SpecValidationError) as raised:
        parse_spec_document(payload)
    return str(raised.value)


def test_live_internet_is_eval_only(example_payload):
    payload = set_path(example_payload, "world.network.internet", "live")
    assert "live internet is eval only" in rejection_text(payload)


def test_judge_rubric_needs_a_judge_endpoint(example_payload):
    assert "judge rubric" in rejection_text(set_path(example_payload, "run.judge", None))


def test_rl_runs_use_train_split_tasks(example_payload):
    assert "train split" in rejection_text(set_path(example_payload, "task.split", "eval"))


def test_the_action_format_must_express_every_allowed_modality(example_payload):
    payload = set_path(example_payload, "task.allowed_actions", ["computer", "terminal", "control"])
    assert "text_actions cannot express terminal" in rejection_text(payload)
    payload = set_path(payload, "run.policy.action_format", "computer_tool")
    assert parse_spec_document(payload).run.policy.action_format == "computer_tool"


def test_sweep_paths_must_exist(example_payload):
    payload = set_path(example_payload, "experiment.sweep", {"task.difficulty.typos": [1, 2]})
    assert "task.difficulty.typos does not exist" in rejection_text(payload)


def test_sweep_paths_may_target_unset_optional_leaves(example_payload):
    payload = set_path(example_payload, "experiment.sweep", {"world.clock.fixed": [None]})
    assert parse_spec_document(payload).experiment.sweep == {"world.clock.fixed": [None]}


def test_errors_name_the_failing_location(example_payload):
    with pytest.raises(SpecValidationError) as raised:
        parse_spec_document(set_path(example_payload, "image.display.width", 10))
    assert any(problem.startswith("image.display.width:") for problem in raised.value.problems)


def test_yaml_round_trip_preserves_the_document(example_spec_text):
    document = load_spec_document(example_spec_text)
    dumped_text = dump_spec_document(document)
    assert load_spec_document(dumped_text) == document
    assert dumped_text.startswith("spec_version: 1")


def test_non_mapping_yaml_is_rejected():
    with pytest.raises(SpecValidationError) as raised:
        load_spec_document("- just\n- a list\n")
    assert "YAML mapping" in str(raised.value)


def test_path_helpers():
    payload = {"task": {"difficulty": {"distractors": 5}, "judge_rubric": None}}
    assert path_exists(payload, "task.difficulty.distractors")
    assert path_exists(payload, "task.judge_rubric")
    assert not path_exists(payload, "task.difficulty.typos")
    assert not path_exists(payload, "task.judge_rubric.version")
    updated = set_path(payload, "task.difficulty.distractors", 9)
    assert updated["task"]["difficulty"]["distractors"] == 9
    assert payload["task"]["difficulty"]["distractors"] == 5
    with pytest.raises(KeyError):
        set_path(payload, "task.judge_rubric.version", 2)
