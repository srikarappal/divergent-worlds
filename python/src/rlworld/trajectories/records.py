"""Step and episode records (DESIGN.md section 6.11).

An episode is identified by the tuple (image snapshot digest, world digest,
task digest, run id, seed, policy version). Large payloads (prompts, states,
screenshots) live in content addressed blobs and are referenced by digest.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from rlworld.actions.models import Observation
from rlworld.gateway.messages import TokenTrace
from rlworld.rewards.records import RewardRecord, StateChange


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EpisodeKey(FrozenModel):
    image_snapshot_digest: str
    world_digest: str
    task_digest: str
    run_id: str
    seed: int
    policy_version: NonNegativeInt


class StepTimings(FrozenModel):
    model_seconds: float = 0.0
    env_seconds: float = 0.0
    settle_seconds: float = 0.0


class ModelTurn(FrozenModel):
    prompt_ref: str | None = None
    output_text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    token_trace: TokenTrace | None = None
    served_model: str = ""
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cost_usd: float | None = None


class StepRecord(FrozenModel):
    step_index: NonNegativeInt
    observation: Observation
    model_turn: ModelTurn = Field(default_factory=ModelTurn)
    actions: tuple[dict[str, Any], ...] = ()
    parse_error: str | None = None
    state_changes: tuple[StateChange, ...] = ()
    timings: StepTimings = Field(default_factory=StepTimings)
    vm_events: tuple[str, ...] = ()


class EpisodeRecord(FrozenModel):
    episode_id: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    key: EpisodeKey
    task_id: str
    task_template: str
    difficulty_key: str = ""
    instruction: str = ""
    group_id: str | None = None
    sample_index: NonNegativeInt = 0
    run_type: Literal["rl", "eval", "data"]
    initial_state_ref: str | None = None
    ground_truth_ref: str | None = None
    final_state_ref: str | None = None
    final_snapshot_ref: str | None = None
    reward: RewardRecord | None = None
    human_intervened: bool = False
    retention: Literal["full", "thumbnail"] = "full"
    started_at: str
    finished_at: str | None = None
    steps: tuple[StepRecord, ...] = ()

    @property
    def termination(self) -> str | None:
        return self.reward.termination if self.reward else None

    @property
    def is_trainable_success(self) -> bool:
        return (
            self.reward is not None
            and self.reward.counts_for_training
            and self.reward.termination == "success"
            and not self.human_intervened
        )
