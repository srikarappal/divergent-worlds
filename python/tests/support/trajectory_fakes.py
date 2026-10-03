"""Builders for trajectory tests: episodes, steps and reward records."""

from rlworld.actions.models import Observation
from rlworld.gateway.messages import TokenTrace
from rlworld.rewards.records import Provenance, RewardPolicy, RewardRecord
from rlworld.trajectories import EpisodeKey, EpisodeRecord, ModelTurn, StepRecord


def reward(total: float = 1.0, termination: str = "success", counts_for_training: bool = True) -> RewardRecord:
    provenance = Provenance(state_hash="sha256:s", ground_truth_digest="sha256:g", reward_policy=RewardPolicy())
    return RewardRecord(total=total, components={"outcome": total}, termination=termination,
                        counts_for_training=counts_for_training, provenance=provenance)


def episode(episode_id: str = "episode_1", run_id: str = "run_1", task_id: str = "task_1",
            template: str = "tasks.finance.reconcile_invoices", difficulty_key: str = "easy",
            group_id: str | None = "group_1", run_type: str = "rl", **updates) -> EpisodeRecord:
    record = EpisodeRecord(
        episode_id=episode_id,
        key=EpisodeKey(image_snapshot_digest="sha256:img", world_digest="sha256:world", task_digest="sha256:task",
                       run_id=run_id, seed=7, policy_version=3),
        task_id=task_id,
        task_template=template,
        difficulty_key=difficulty_key,
        instruction="Reconcile the invoices.",
        group_id=group_id,
        run_type=run_type,
        started_at="2026-10-03T10:00:00.000+00:00",
    )
    return record.model_copy(update=updates)


def step(step_index: int, screenshot_hash: str = "aa", actions=(), output_text: str = "Action: wait()",
         with_tokens: bool = True, parse_error: str | None = None) -> StepRecord:
    observation = Observation.model_validate(
        {
            "step_index": step_index,
            "screenshot": {"uri": f"s3://frames/{step_index}.webp", "width": 1280, "height": 800,
                           "sha256": screenshot_hash},
        }
    )
    token_trace = TokenTrace(prompt_token_ids=(1, 2), completion_token_ids=(3,), completion_logprobs=(-0.5,))
    return StepRecord(
        step_index=step_index,
        observation=observation,
        model_turn=ModelTurn(prompt_ref=f"sha256:prompt{step_index}", output_text=output_text,
                             token_trace=token_trace if with_tokens else None, served_model="policy_v3"),
        actions=tuple(actions),
        parse_error=parse_error,
    )
