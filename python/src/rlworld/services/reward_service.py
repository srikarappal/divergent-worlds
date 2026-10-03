"""Reward service: the finish pipeline (DESIGN.md section 6.9).

Order matters: freeze input and extract state through fleetd, optionally keep a
final snapshot for audits, verify and judge, record the episode and only then
close the lease so the env is torn down. A failure anywhere becomes a recorded
env error, which is excluded from training; the lease is still closed.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from rlworld.rewards.pipeline import JudgeTier, env_error_record, evaluate_episode
from rlworld.rewards.records import EpisodeOutcome, RewardPolicy, RewardRecord
from rlworld.rewards.verifiers import VerifierRegistry
from rlworld.specs.models import TaskSpec
from rlworld.trajectories.exports import retention_class
from rlworld.trajectories.trajectory_store import TrajectoryWriter


class FleetFinisher(Protocol):
    def finish(self, lease_id: str, apps: list[str]) -> dict[str, Any]: ...

    def checkpoint(self, lease_id: str) -> dict[str, str]: ...

    def close(self, lease_id: str) -> None: ...


@dataclass(frozen=True)
class EpisodeContext:
    lease_id: str
    task: TaskSpec
    apps: tuple[str, ...]
    initial_state: dict[str, Any]
    ground_truth: dict[str, Any]
    instruction: str = ""
    screenshots: tuple[str, ...] = ()
    run_type: Literal["rl", "eval", "data"] = "rl"


class RewardService:
    def __init__(
        self,
        fleet: FleetFinisher,
        registry: VerifierRegistry,
        judge: JudgeTier | None = None,
        policy: RewardPolicy | None = None,
        volatile_patterns: Sequence[str] = (),
        audit_snapshot_rate: float = 0.0,
        full_retention_rate: float = 0.05,
        random_generator: random.Random | None = None,
    ) -> None:
        self.fleet = fleet
        self.registry = registry
        self.judge = judge
        self.policy = policy
        self.volatile_patterns = volatile_patterns
        self.audit_snapshot_rate = audit_snapshot_rate
        self.full_retention_rate = full_retention_rate
        self.random_generator = random_generator or random.Random()

    def audit_snapshot(self, lease_id: str) -> str | None:
        """Best effort: a failed audit snapshot never costs the episode its reward."""
        if self.random_generator.random() >= self.audit_snapshot_rate:
            return None
        try:
            return self.fleet.checkpoint(lease_id).get("state_path")
        except Exception:  # audits are optional; the episode must still be scored and closed
            return None

    def score(self, context: EpisodeContext, outcome: EpisodeOutcome, final_state: dict[str, Any]) -> RewardRecord:
        try:
            return evaluate_episode(
                context.task,
                self.registry,
                context.initial_state,
                final_state,
                context.ground_truth,
                outcome,
                self.policy,
                self.judge,
                self.volatile_patterns,
                instruction=context.instruction,
                screenshots=context.screenshots,
            )
        except Exception as error:  # misconfiguration at reward time is an env error, never a zero reward
            return env_error_record(f"evaluation failed: {type(error).__name__}: {error}", final_state,
                                    context.ground_truth, self.policy)

    def finish_episode(
        self, context: EpisodeContext, outcome: EpisodeOutcome, writer: TrajectoryWriter | None = None
    ) -> RewardRecord:
        final_state: dict[str, Any] = {}
        final_snapshot_ref = None
        try:
            try:
                final_state = self.fleet.finish(context.lease_id, list(context.apps))
            except Exception as error:  # the env could not freeze or extract: an env error
                reward = env_error_record(f"finish failed: {type(error).__name__}: {error}", final_state,
                                          context.ground_truth, self.policy)
            else:
                final_snapshot_ref = self.audit_snapshot(context.lease_id)
                reward = self.score(context, outcome, final_state)
            if writer is not None:
                scored_episode = writer.episode.model_copy(update={"reward": reward})
                retention = retention_class(scored_episode, self.full_retention_rate, self.random_generator.random())
                writer.finish(reward, final_state, final_snapshot_ref=final_snapshot_ref, retention=retention)
            return reward
        finally:
            try:
                self.fleet.close(context.lease_id)
            except Exception:  # an expired lease is already torn down
                pass
