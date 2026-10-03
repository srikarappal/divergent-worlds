import pytest
from support.trajectory_fakes import episode, reward, step

from rlworld.actions.models import ClickAction, DoneAction, Observation
from rlworld.rewards import CheckResult, FunctionVerifier, VerifierRegistry
from rlworld.rewards.checks import cells_equal
from rlworld.specs import parse_spec_document
from rlworld.trajectories import (
    InMemoryObjectStore,
    TrajectoryStore,
    measure_divergence,
    recorded_outcome,
    replay_frames,
    rescore_episode,
)

INITIAL_STATE = {"files": {"reconciliation.xlsx": {"cells": {"B2": {"value": None}}}}}
FINAL_STATE = {"files": {"reconciliation.xlsx": {"cells": {"B2": {"value": 1250.0}}}}}


def workbook_cells(context):
    return context.final_state["files"]["reconciliation.xlsx"]["cells"]


def check_total_lenient(context):
    return [cells_equal("total", workbook_cells(context), {"B2": 1250}, numeric_tolerance=100)]


def check_total_strict(context):
    return [cells_equal("total", workbook_cells(context), {"B2": context.ground_truth["total"]})]


def check_nothing(context):
    return [CheckResult(name="placeholder", tier="semantic", passed=True)]


def registry_with(exact_function, version):
    registry = VerifierRegistry()
    registry.register(FunctionVerifier("verifiers.excel.reconciliation_values_match", version, "exact", exact_function))
    registry.register(FunctionVerifier("verifiers.mail.summary_lists_discrepant_invoices", "1", "semantic",
                                       check_nothing))
    return registry


def recorded_episode(trajectory_store, total_in_ground_truth=1250.0):
    writer = trajectory_store.start_episode(episode(), INITIAL_STATE, {"total": total_in_ground_truth})
    writer.write_step(step(0, actions=[{"name": "click", "x": 5, "y": 6}]))
    writer.write_step(step(1, actions=[{"name": "answer", "text": "1250"}]))
    writer.finish(reward(), FINAL_STATE)
    return trajectory_store.load_episode("run_1", "episode_1")


def test_exact_replay_returns_recorded_frames():
    loaded_episode = recorded_episode(TrajectoryStore(InMemoryObjectStore()))
    frames = list(replay_frames(loaded_episode))
    assert [frame.step_index for frame in frames] == [0, 1]
    assert frames[0].actions == (ClickAction(x=5, y=6),)
    assert frames[1].observation.screenshot.uri == "s3://frames/1.webp"


def test_recorded_outcome_reads_the_last_step():
    outcome = recorded_outcome(episode(steps=(step(0), step(1, actions=[{"name": "answer", "text": "42"}]))))
    assert (outcome.steps_taken, outcome.final_action, outcome.answer_text) == (2, "answer", "42")
    assert recorded_outcome(episode(steps=(step(0, parse_error="bad action"),))).agent_error == "bad action"
    assert recorded_outcome(episode(steps=(step(0, actions=[DoneAction().model_dump()]),))).final_action == "done"


def test_rescoring_applies_todays_verifiers(example_payload):
    task = parse_spec_document(example_payload).task.model_copy(update={"judge_rubric": None})
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    loaded_episode = recorded_episode(trajectory_store, total_in_ground_truth=1251.0)
    lenient_record = rescore_episode(trajectory_store, loaded_episode, task, registry_with(check_total_lenient, "1"))
    strict_record = rescore_episode(trajectory_store, loaded_episode, task, registry_with(check_total_strict, "2"))
    assert lenient_record.termination == "success"
    assert strict_record.termination == "failure"
    assert strict_record.provenance.verifier_versions["verifiers.excel.reconciliation_values_match"] == "2"


def test_env_error_episodes_cannot_be_rescored(example_payload):
    task = parse_spec_document(example_payload).task
    broken_episode = episode(reward=reward(0.0, "env_error", counts_for_training=False))
    with pytest.raises(ValueError):
        rescore_episode(TrajectoryStore(InMemoryObjectStore()), broken_episode, task, VerifierRegistry())


def rerun_observation(step_index, screenshot_hash):
    return Observation.model_validate(
        {"step_index": step_index,
         "screenshot": {"uri": "s3://rerun.webp", "width": 1280, "height": 800, "sha256": screenshot_hash}}
    )


def test_divergence_counts_matching_steps():
    recorded_steps = [step(0, "aa"), step(1, "bb"), step(2, "cc")]
    identical = measure_divergence(recorded_steps, [rerun_observation(index, value)
                                                    for index, value in enumerate(["aa", "bb", "cc"])])
    assert (identical.divergence_rate, identical.first_divergent_step) == (0.0, None)
    drifted = measure_divergence(recorded_steps, [rerun_observation(index, value)
                                                  for index, value in enumerate(["aa", "zz", "cc"])])
    assert drifted.matching_steps == 2
    assert drifted.first_divergent_step == 1
    assert drifted.divergence_rate == pytest.approx(1 / 3)
    truncated = measure_divergence(recorded_steps, [rerun_observation(0, "aa")])
    assert truncated.first_divergent_step == 1
