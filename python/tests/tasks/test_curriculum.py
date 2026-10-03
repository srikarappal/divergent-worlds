from collections import Counter

import pytest

from rlworld.rewards import Provenance, RewardPolicy, RewardRecord
from rlworld.specs.models import DifficultySpec
from rlworld.tasks.curriculum import (
    CandidateTask,
    CurriculumPlanner,
    CurriculumSampler,
    PassRateTracker,
    difficulty_key,
    easier,
    filter_informative_groups,
    harder,
    mixed_group_probability,
    record_group_outcomes,
)

TEMPLATE = "tasks.finance.reconcile_invoices"
BASE_DIFFICULTY = DifficultySpec(apps=3, oracle_steps=30, distractors=5, corrupted_inputs=1, ambiguity="low",
                                 dependency_depth=2)


def reward(total: float, termination: str = "success", counts_for_training: bool = True) -> RewardRecord:
    provenance = Provenance(state_hash="sha256:x", ground_truth_digest="sha256:y", reward_policy=RewardPolicy())
    return RewardRecord(total=total, components={}, termination=termination,
                        counts_for_training=counts_for_training, provenance=provenance)


SUCCESS = reward(1.0)
FAILURE = reward(0.0, "failure")
ENV_ERROR = reward(0.0, "env_error", counts_for_training=False)


def test_difficulty_key_names_every_axis():
    assert difficulty_key(BASE_DIFFICULTY) == "apps=3,steps=30,ambiguity=low,distractors=5,corrupted=1,depth=2"


def test_mixed_group_probability():
    assert mixed_group_probability(0.5, 8) == pytest.approx(1 - 2 / 256)
    assert mixed_group_probability(0.0, 8) == 0.0
    assert mixed_group_probability(1.0, 8) == 0.0
    assert mixed_group_probability(0.95, 8) < mixed_group_probability(0.6, 8)
    assert mixed_group_probability(0.5, 1) == 1.0


def test_unknown_keys_start_at_the_prior():
    tracker = PassRateTracker()
    assert tracker.pass_rate(TEMPLATE, "any", policy_version=3) == 0.5
    assert tracker.observations(TEMPLATE, "any", policy_version=3) == 0.0


def test_old_policy_evidence_fades():
    tracker = PassRateTracker(decay_per_version=0.5)
    tracker.record(TEMPLATE, "bucket", policy_version=1, successes=10, attempts=10)
    tracker.record(TEMPLATE, "bucket", policy_version=5, successes=0, attempts=10)
    expected_rate = (10 * 0.5**4 + 1) / (10 * 0.5**4 + 10 + 2)
    assert tracker.pass_rate(TEMPLATE, "bucket", policy_version=5) == pytest.approx(expected_rate)
    assert tracker.observations(TEMPLATE, "bucket", policy_version=6) == pytest.approx((10 * 0.5**4 + 10) * 0.5)


def test_late_results_from_older_policies_count_less():
    tracker = PassRateTracker(decay_per_version=0.5)
    tracker.record(TEMPLATE, "bucket", policy_version=4, successes=0, attempts=4)
    tracker.record(TEMPLATE, "bucket", policy_version=3, successes=4, attempts=4)
    assert tracker.observations(TEMPLATE, "bucket", policy_version=4) == pytest.approx(6.0)
    with pytest.raises(ValueError):
        tracker.record(TEMPLATE, "bucket", policy_version=4, successes=5, attempts=4)


def test_group_outcomes_ignore_env_errors():
    tracker = PassRateTracker()
    candidate = CandidateTask("task_1", TEMPLATE, BASE_DIFFICULTY)
    record_group_outcomes(tracker, candidate, 2, [SUCCESS, FAILURE, ENV_ERROR, SUCCESS])
    assert tracker.observations(TEMPLATE, difficulty_key(BASE_DIFFICULTY), 2) == 3
    assert tracker.pass_rate(TEMPLATE, difficulty_key(BASE_DIFFICULTY), 2) == pytest.approx(3 / 5)


def test_zero_advantage_and_undersized_groups_are_dropped():
    result = filter_informative_groups(
        [
            [SUCCESS, FAILURE, SUCCESS],
            [SUCCESS, SUCCESS, SUCCESS],
            [FAILURE, FAILURE],
            [SUCCESS, ENV_ERROR, ENV_ERROR],
            [reward(0.5), reward(0.5)],
            [SUCCESS, ENV_ERROR, FAILURE],
        ]
    )
    assert result.zero_advantage_groups == 3
    assert result.undersized_groups == 1
    assert result.kept_groups == ((SUCCESS, FAILURE, SUCCESS), (SUCCESS, FAILURE))


def candidates_with_rates(tracker: PassRateTracker) -> dict[str, CandidateTask]:
    candidates = {}
    for name, distractors, successes in [("easy", 0, 99), ("hard", 20, 1), ("frontier", 8, 50)]:
        difficulty = BASE_DIFFICULTY.model_copy(update={"distractors": distractors})
        tracker.record(TEMPLATE, difficulty_key(difficulty), policy_version=1, successes=successes, attempts=100)
        candidates[name] = CandidateTask(name, TEMPLATE, difficulty)
    return candidates


def test_the_sampler_prefers_tasks_with_mixed_groups():
    tracker = PassRateTracker()
    candidates = candidates_with_rates(tracker)
    sampler = CurriculumSampler(tracker, group_size=8, seed=7)
    assert sampler.task_weight(candidates["frontier"], 1) > 0.99
    assert sampler.task_weight(candidates["easy"], 1) == pytest.approx(mixed_group_probability(100 / 102, 8))
    assert CurriculumSampler(tracker, group_size=8, exploration_floor=0.3).task_weight(candidates["easy"], 1) == 0.3
    first_picks = Counter(sampler.sample(list(candidates.values()), 1, 1)[0].task_id for _ in range(400))
    assert first_picks["frontier"] > 260
    assert first_picks["easy"] > 0 and first_picks["hard"] > 0


def test_sampling_is_seeded_and_without_replacement():
    tracker = PassRateTracker()
    candidates = list(candidates_with_rates(tracker).values())
    first_batch = CurriculumSampler(tracker, group_size=8, seed=3).sample(candidates, 2, 1)
    second_batch = CurriculumSampler(tracker, group_size=8, seed=3).sample(candidates, 2, 1)
    assert first_batch == second_batch
    assert len({candidate.task_id for candidate in first_batch}) == 2
    assert len(CurriculumSampler(tracker, group_size=8).sample(candidates, 10, 1)) == 3


def test_harder_and_easier_step_one_axis_group_at_a_time():
    assert harder(BASE_DIFFICULTY).distractors == 7
    assert harder(BASE_DIFFICULTY).corrupted_inputs == 2
    noisy = BASE_DIFFICULTY.model_copy(update={"distractors": 50, "corrupted_inputs": 10})
    assert harder(noisy).dependency_depth == 3
    maxed = noisy.model_copy(update={"dependency_depth": 6, "ambiguity": "high"})
    assert harder(maxed) is None
    clean = BASE_DIFFICULTY.model_copy(update={"distractors": 0, "corrupted_inputs": 0})
    assert easier(clean).dependency_depth == 1
    assert easier(clean.model_copy(update={"dependency_depth": 0})).ambiguity == "none"
    simplest = DifficultySpec(apps=1, oracle_steps=5)
    assert easier(simplest) is None
    assert easier(simplest.model_copy(update={"oracle_steps": 40})).oracle_steps == 30


def test_the_planner_targets_the_frontier_then_steps_difficulty():
    tracker = PassRateTracker()
    planner = CurriculumPlanner(tracker, tasks_per_request=4)
    easy_difficulty = BASE_DIFFICULTY.model_copy(update={"distractors": 0})
    hard_difficulty = BASE_DIFFICULTY.model_copy(update={"distractors": 20})
    frontier_difficulty = BASE_DIFFICULTY
    tracker.record("easy_template", difficulty_key(easy_difficulty), 1, successes=95, attempts=100)
    tracker.record("hard_template", difficulty_key(hard_difficulty), 1, successes=2, attempts=100)
    tracker.record("mixed_template", difficulty_key(frontier_difficulty), 1, successes=40, attempts=100)
    tracker.record("mixed_template", difficulty_key(easy_difficulty), 1, successes=99, attempts=100)
    tracker.record("unseen_template", difficulty_key(easy_difficulty), 1, successes=2, attempts=2)
    requests = planner.plan(
        {
            "easy_template": [easy_difficulty],
            "hard_template": [hard_difficulty],
            "mixed_template": [frontier_difficulty, easy_difficulty],
            "unseen_template": [easy_difficulty],
        },
        policy_version=1,
    )
    summary = [(request.template, request.reason, request.difficulty.distractors, request.count)
               for request in requests]
    assert summary == [
        ("easy_template", "harder", 2, 4),
        ("hard_template", "easier", 18, 4),
        ("mixed_template", "frontier", 5, 4),
    ]
