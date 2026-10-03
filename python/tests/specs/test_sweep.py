import pytest

from rlworld.specs import SpecValidationError, expand_sweep, parse_spec_document, run_spec_digest, set_path


def random_sweep_payload(example_payload, sample_count: int, seed: int = 0):
    payload = set_path(example_payload, "experiment.sweep_mode", "random")
    payload = set_path(payload, "experiment.random_samples", sample_count)
    return set_path(payload, "experiment.random_seed", seed)


def test_grid_sweep_expands_every_combination(example_payload):
    points = expand_sweep(parse_spec_document(example_payload))
    assert len(points) == 6
    assert [point.run_name for point in points] == [f"recon_difficulty_sweep__{index:03d}" for index in range(6)]
    assert points[0].assignments == {"task.difficulty.corrupted_inputs": 0, "task.difficulty.distractors": 0}
    assert points[1].assignments == {"task.difficulty.corrupted_inputs": 0, "task.difficulty.distractors": 5}
    assert points[-1].assignments == {"task.difficulty.corrupted_inputs": 2, "task.difficulty.distractors": 10}
    for point in points:
        assert point.document.task.difficulty.distractors == point.assignments["task.difficulty.distractors"]
        assert point.document.experiment.sweep == {}
    assert len({point.run_digest for point in points}) == 6


def test_grid_sweep_is_deterministic(example_payload):
    document = parse_spec_document(example_payload)
    assert [point.run_digest for point in expand_sweep(document)] == [
        point.run_digest for point in expand_sweep(document)
    ]


def test_random_sweep_is_deterministic_per_seed(example_payload):
    document = parse_spec_document(random_sweep_payload(example_payload, sample_count=3))
    first_points = expand_sweep(document)
    second_points = expand_sweep(document)
    assert [point.assignments for point in first_points] == [point.assignments for point in second_points]
    assert len(first_points) == 3
    assert len({tuple(sorted(point.assignments.items())) for point in first_points}) == 3
    for point in first_points:
        assert point.document.experiment.sweep_mode == "grid"
        assert point.document.experiment.random_samples is None


def test_random_sweep_caps_at_the_grid_size(example_payload):
    document = parse_spec_document(random_sweep_payload(example_payload, sample_count=50))
    assert len(expand_sweep(document)) == 6


def test_invalid_sweep_values_name_the_point(example_payload):
    payload = set_path(example_payload, "experiment.sweep", {"task.difficulty.distractors": [0, -1]})
    with pytest.raises(SpecValidationError) as raised:
        expand_sweep(parse_spec_document(payload))
    assert "sweep point 1 (task.difficulty.distractors=-1)" in str(raised.value)


def test_no_sweep_yields_one_point(example_payload):
    document = parse_spec_document(set_path(example_payload, "experiment.sweep", {}))
    points = expand_sweep(document)
    assert len(points) == 1
    assert points[0].assignments == {}
    assert points[0].run_digest == run_spec_digest(document)
