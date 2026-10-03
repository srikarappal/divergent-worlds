"""Reward data: checks, state changes, policies, outcomes and reward records (DESIGN.md section 6.9)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

CheckTier = Literal["exact", "semantic", "judge"]
TerminationLabel = Literal["success", "failure", "max_steps", "agent_error", "env_error"]
FinalAction = Literal["answer", "done", "give_up"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckResult(FrozenModel):
    name: str
    tier: CheckTier
    passed: bool
    weight: float = Field(default=1.0, ge=0.0)
    detail: str = ""
    expected: Any = None
    actual: Any = None


class StateChange(FrozenModel):
    path: str
    kind: Literal["added", "removed", "modified"]
    before: Any = None
    after: Any = None


class RewardPolicy(FrozenModel):
    """Binary outcome behind a side effect gate unless a task has validated partial credit."""

    mode: Literal["binary", "partial_credit"] = "binary"
    side_effect_mode: Literal["gate", "penalty"] = "gate"
    side_effect_penalty: float = Field(default=0.25, ge=0.0, le=1.0)
    max_side_effect_penalty: float = Field(default=1.0, ge=0.0, le=1.0)
    efficiency_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    require_finish: bool = False


class EpisodeOutcome(FrozenModel):
    """How the episode ended, as reported by the rollout worker and the env."""

    steps_taken: NonNegativeInt
    final_action: FinalAction | None = None
    answer_text: str | None = None
    agent_error: str | None = None
    env_error: str | None = None


class Provenance(FrozenModel):
    verifier_versions: dict[str, str] = Field(default_factory=dict)
    judge_model: str | None = None
    rubric_version: str | None = None
    judge_skipped: bool = False
    state_hash: str
    ground_truth_digest: str
    reward_policy: RewardPolicy


class RewardRecord(FrozenModel):
    total: float
    components: dict[str, float]
    termination: TerminationLabel
    counts_for_training: bool
    checks: tuple[CheckResult, ...] = ()
    side_effect_violations: tuple[StateChange, ...] = ()
    error_detail: str | None = None
    provenance: Provenance
