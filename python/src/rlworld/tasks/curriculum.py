"""Curriculum (DESIGN.md section 6.10).

Pass rates are tracked per task template and difficulty setting, decayed across
policy versions so old evidence fades as the policy improves. Sampling weights
come from the chance that a GRPO group of size k mixes successes and failures,
1 - p^k - (1 - p)^k, which is exactly the chance the group carries signal; a
small floor keeps easy and hard tasks under watch. Groups whose rewards are all
equal carry zero advantage and are dropped. The planner asks generators for
more tasks where pass rates sit in the learnable band and steps difficulty up
or down when a template has none there.
"""

import random
import threading
from collections.abc import Mapping, Sequence
from operator import itemgetter
from dataclasses import dataclass
from typing import Literal

from rlworld.rewards.records import RewardRecord
from rlworld.specs.models import DifficultySpec

AMBIGUITY_LEVELS = ("none", "low", "medium", "high")
MAX_DISTRACTORS = 50
MAX_CORRUPTED_INPUTS = 10
MAX_DEPENDENCY_DEPTH = 6


@dataclass(frozen=True)
class CandidateTask:
    task_id: str
    template: str
    difficulty: DifficultySpec


@dataclass(frozen=True)
class GenerationRequest:
    template: str
    difficulty: DifficultySpec
    reason: Literal["frontier", "harder", "easier"]
    count: int


@dataclass(frozen=True)
class GroupFilterResult:
    kept_groups: tuple[tuple[RewardRecord, ...], ...]
    zero_advantage_groups: int
    undersized_groups: int


@dataclass
class WeightedCounts:
    successes: float
    attempts: float
    policy_version: int


def difficulty_key(difficulty: DifficultySpec) -> str:
    return (
        f"apps={difficulty.apps},steps={difficulty.oracle_steps},ambiguity={difficulty.ambiguity},"
        f"distractors={difficulty.distractors},corrupted={difficulty.corrupted_inputs},"
        f"depth={difficulty.dependency_depth}"
    )


def difficulty_score(difficulty: DifficultySpec) -> float:
    """A scalar for ordering difficulty settings of one template."""
    return (
        difficulty.apps
        + difficulty.oracle_steps / 10
        + difficulty.distractors / 2
        + difficulty.corrupted_inputs
        + 2 * difficulty.dependency_depth
        + 2 * AMBIGUITY_LEVELS.index(difficulty.ambiguity)
    )


def mixed_group_probability(pass_rate: float, group_size: int) -> float:
    """Chance a group holds both a success and a failure; with one rollout, the scaled Bernoulli variance."""
    if group_size < 2:
        return 4 * pass_rate * (1 - pass_rate)
    return 1 - pass_rate**group_size - (1 - pass_rate) ** group_size


class PassRateTracker:
    def __init__(self, decay_per_version: float = 0.7, prior_successes: float = 1.0, prior_failures: float = 1.0) -> None:
        if not 0 < decay_per_version <= 1:
            raise ValueError("decay_per_version must be in (0, 1]")
        self.decay_per_version = decay_per_version
        self.prior_successes = prior_successes
        self.prior_failures = prior_failures
        self.counts: dict[tuple[str, str], WeightedCounts] = {}
        self.lock = threading.Lock()

    def record(self, template: str, bucket: str, policy_version: int, successes: int, attempts: int) -> None:
        if not 0 <= successes <= attempts:
            raise ValueError("successes must be between 0 and attempts")
        with self.lock:
            counts = self.counts.get((template, bucket))
            if counts is None:
                self.counts[(template, bucket)] = WeightedCounts(successes, attempts, policy_version)
                return
            if policy_version >= counts.policy_version:
                carry_over = self.decay_per_version ** (policy_version - counts.policy_version)
                counts.successes = counts.successes * carry_over + successes
                counts.attempts = counts.attempts * carry_over + attempts
                counts.policy_version = policy_version
            else:
                late_weight = self.decay_per_version ** (counts.policy_version - policy_version)
                counts.successes += successes * late_weight
                counts.attempts += attempts * late_weight

    def decayed_counts(self, template: str, bucket: str, policy_version: int) -> tuple[float, float]:
        with self.lock:
            counts = self.counts.get((template, bucket))
            if counts is None:
                return 0.0, 0.0
            carry_over = self.decay_per_version ** max(0, policy_version - counts.policy_version)
            return counts.successes * carry_over, counts.attempts * carry_over

    def pass_rate(self, template: str, bucket: str, policy_version: int) -> float:
        successes, attempts = self.decayed_counts(template, bucket, policy_version)
        return (successes + self.prior_successes) / (attempts + self.prior_successes + self.prior_failures)

    def observations(self, template: str, bucket: str, policy_version: int) -> float:
        return self.decayed_counts(template, bucket, policy_version)[1]


def record_group_outcomes(
    tracker: PassRateTracker, candidate: CandidateTask, policy_version: int, records: Sequence[RewardRecord]
) -> None:
    trainable_records = [record for record in records if record.counts_for_training]
    if not trainable_records:
        return
    successes = sum(1 for record in trainable_records if record.termination == "success")
    tracker.record(candidate.template, difficulty_key(candidate.difficulty), policy_version, successes,
                   len(trainable_records))


def filter_informative_groups(
    groups: Sequence[Sequence[RewardRecord]], minimum_group_size: int = 2
) -> GroupFilterResult:
    """Drop env errors, then groups too small to compare and groups whose rewards are all equal."""
    kept_groups = []
    zero_advantage_groups = 0
    undersized_groups = 0
    for group in groups:
        trainable_records = tuple(record for record in group if record.counts_for_training)
        if len(trainable_records) < minimum_group_size:
            undersized_groups += 1
        elif len({record.total for record in trainable_records}) == 1:
            zero_advantage_groups += 1
        else:
            kept_groups.append(trainable_records)
    return GroupFilterResult(tuple(kept_groups), zero_advantage_groups, undersized_groups)


class CurriculumSampler:
    def __init__(
        self,
        tracker: PassRateTracker,
        group_size: int,
        exploration_floor: float = 0.05,
        seed: int = 0,
    ) -> None:
        self.tracker = tracker
        self.group_size = group_size
        self.exploration_floor = exploration_floor
        self.random_generator = random.Random(seed)

    def task_weight(self, candidate: CandidateTask, policy_version: int) -> float:
        pass_rate = self.tracker.pass_rate(candidate.template, difficulty_key(candidate.difficulty), policy_version)
        return max(self.exploration_floor, mixed_group_probability(pass_rate, self.group_size))

    def sample(self, candidates: Sequence[CandidateTask], batch_size: int, policy_version: int) -> list[CandidateTask]:
        """Weighted sampling without replacement (Efraimidis and Spirakis keys)."""
        keyed_candidates = [
            (self.random_generator.random() ** (1 / self.task_weight(candidate, policy_version)), candidate)
            for candidate in candidates
        ]
        keyed_candidates.sort(key=itemgetter(0), reverse=True)
        return [candidate for _, candidate in keyed_candidates[:batch_size]]


def harder(difficulty: DifficultySpec) -> DifficultySpec | None:
    """One step up: more noise first, then deeper cross app dependencies, then more ambiguity."""
    if difficulty.distractors < MAX_DISTRACTORS or difficulty.corrupted_inputs < MAX_CORRUPTED_INPUTS:
        return difficulty.model_copy(
            update={
                "distractors": min(MAX_DISTRACTORS, difficulty.distractors + 2),
                "corrupted_inputs": min(MAX_CORRUPTED_INPUTS, difficulty.corrupted_inputs + 1),
            }
        )
    if difficulty.dependency_depth < MAX_DEPENDENCY_DEPTH:
        return difficulty.model_copy(update={"dependency_depth": difficulty.dependency_depth + 1})
    ambiguity_index = AMBIGUITY_LEVELS.index(difficulty.ambiguity)
    if ambiguity_index + 1 < len(AMBIGUITY_LEVELS):
        return difficulty.model_copy(update={"ambiguity": AMBIGUITY_LEVELS[ambiguity_index + 1]})
    return None


def easier(difficulty: DifficultySpec) -> DifficultySpec | None:
    """One step down: less noise first, then shallower dependencies, less ambiguity, fewer steps."""
    if difficulty.distractors or difficulty.corrupted_inputs:
        return difficulty.model_copy(
            update={
                "distractors": max(0, difficulty.distractors - 2),
                "corrupted_inputs": max(0, difficulty.corrupted_inputs - 1),
            }
        )
    if difficulty.dependency_depth:
        return difficulty.model_copy(update={"dependency_depth": difficulty.dependency_depth - 1})
    ambiguity_index = AMBIGUITY_LEVELS.index(difficulty.ambiguity)
    if ambiguity_index:
        return difficulty.model_copy(update={"ambiguity": AMBIGUITY_LEVELS[ambiguity_index - 1]})
    if difficulty.oracle_steps > 5:
        return difficulty.model_copy(update={"oracle_steps": max(5, difficulty.oracle_steps * 3 // 4)})
    return None


class CurriculumPlanner:
    def __init__(
        self,
        tracker: PassRateTracker,
        learnable_band: tuple[float, float] = (0.2, 0.8),
        minimum_observations: float = 8.0,
        tasks_per_request: int = 16,
    ) -> None:
        self.tracker = tracker
        self.learnable_band = learnable_band
        self.minimum_observations = minimum_observations
        self.tasks_per_request = tasks_per_request

    def plan(
        self, template_difficulties: Mapping[str, Sequence[DifficultySpec]], policy_version: int
    ) -> list[GenerationRequest]:
        lowest_rate, highest_rate = self.learnable_band
        requests = []
        for template, difficulties in sorted(template_difficulties.items()):
            rated_difficulties = []
            for difficulty in difficulties:
                bucket = difficulty_key(difficulty)
                if self.tracker.observations(template, bucket, policy_version) >= self.minimum_observations:
                    rated_difficulties.append((difficulty, self.tracker.pass_rate(template, bucket, policy_version)))
            in_band = [difficulty for difficulty, rate in rated_difficulties if lowest_rate <= rate <= highest_rate]
            too_easy = [difficulty for difficulty, rate in rated_difficulties if rate > highest_rate]
            too_hard = [difficulty for difficulty, rate in rated_difficulties if rate < lowest_rate]
            if in_band:
                requests.extend(
                    GenerationRequest(template, difficulty, "frontier", self.tasks_per_request) for difficulty in in_band
                )
            elif too_easy:
                harder_difficulty = harder(max(too_easy, key=difficulty_score))
                if harder_difficulty is not None:
                    requests.append(GenerationRequest(template, harder_difficulty, "harder", self.tasks_per_request))
            elif too_hard:
                easier_difficulty = easier(min(too_hard, key=difficulty_score))
                if easier_difficulty is not None:
                    requests.append(GenerationRequest(template, easier_difficulty, "easier", self.tasks_per_request))
        return requests
