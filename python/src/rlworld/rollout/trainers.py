"""Trainer adapter shims (DESIGN.md sections 6.8 and 6.12).

Every model turn of a trainable episode in an informative group becomes one
sample: the exact prompt and completion token ids the inference server saw and
produced, the completion logprobs, the episode's reward and the policy version
that sampled it. Prompts change only at their tail but do not extend the
previous turn, so turns are separate samples and only completion tokens train.
The shims lay samples out the way each trainer's rollout interface takes them,
as plain lists in sample order; trainer side glue wraps them into tensors or
structs and loads the turn's images from TurnSample.image_uris.

verl: AgentLoopOutput fields. SkyRL: a step wise GeneratorOutput whose
trajectory ids carry the group id. prime-rl: TrainingSample fields over the
full sequence with GRPO advantages, since this worker replaces its
orchestrator. AReaL: per token rows with versions, one group's rows adjacent.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rlworld.rollout.groups import GroupResult
from rlworld.trajectories.records import EpisodeRecord
from rlworld.trajectories.trajectory_store import TrajectoryStore


@dataclass(frozen=True)
class TurnSample:
    episode_id: str
    group_id: str
    sample_index: int
    turn_index: int
    is_last_turn: bool
    template: str
    prompt_token_ids: tuple[int, ...]
    completion_token_ids: tuple[int, ...]
    completion_logprobs: tuple[float, ...]
    reward: float
    advantage: float
    policy_version: int
    image_uris: tuple[str, ...] = ()
    model_seconds: float = 0.0
    env_seconds: float = 0.0


def is_trainable(episode: EpisodeRecord) -> bool:
    return episode.reward is not None and episode.reward.counts_for_training and not episode.human_intervened


def prompt_image_uris(trajectory_store: TrajectoryStore, prompt_ref: str | None) -> tuple[str, ...]:
    if prompt_ref is None:
        return ()
    uris = []
    for message in trajectory_store.load_json_blob(prompt_ref):
        content = message.get("content")
        if isinstance(content, list):
            uris.extend(part["uri"] for part in content if part.get("type") == "image")
    return tuple(uris)


def turn_samples(group: GroupResult, trajectory_store: TrajectoryStore) -> list[TurnSample]:
    """Samples for every model turn of the group's trainable episodes; advantages are reward minus the group mean."""
    trainable_episodes = [episode for episode in group.episodes if is_trainable(episode)]
    if not trainable_episodes:
        return []
    mean_reward = sum(episode.reward.total for episode in trainable_episodes) / len(trainable_episodes)
    samples = []
    for episode in trainable_episodes:
        steps = [step for step in trajectory_store.load_steps(episode.key.run_id, episode.episode_id)
                 if step.model_turn.token_trace is not None]
        for position, step in enumerate(steps):
            trace = step.model_turn.token_trace
            if len(trace.completion_logprobs) != len(trace.completion_token_ids):
                raise ValueError(f"episode {episode.episode_id} step {step.step_index} has "
                                 f"{len(trace.completion_logprobs)} logprobs for "
                                 f"{len(trace.completion_token_ids)} tokens")
            samples.append(TurnSample(
                episode_id=episode.episode_id,
                group_id=group.group_id,
                sample_index=episode.sample_index,
                turn_index=step.step_index,
                is_last_turn=position == len(steps) - 1,
                template=episode.task_template,
                prompt_token_ids=trace.prompt_token_ids,
                completion_token_ids=trace.completion_token_ids,
                completion_logprobs=trace.completion_logprobs,
                reward=episode.reward.total,
                advantage=episode.reward.total - mean_reward,
                policy_version=episode.key.policy_version,
                image_uris=prompt_image_uris(trajectory_store, step.model_turn.prompt_ref),
                model_seconds=step.timings.model_seconds,
                env_seconds=step.timings.env_seconds,
            ))
    return samples


def within_staleness(samples: Sequence[TurnSample], trainer_version: int, max_staleness: int) -> list[TurnSample]:
    """Drops samples from policies more than max_staleness versions behind the trainer."""
    return [sample for sample in samples if trainer_version - sample.policy_version <= max_staleness]


def verl_agent_loop_outputs(samples: Sequence[TurnSample]) -> list[dict[str, Any]]:
    return [
        {
            "prompt_ids": list(sample.prompt_token_ids),
            "response_ids": list(sample.completion_token_ids),
            "response_mask": [1] * len(sample.completion_token_ids),
            "response_logprobs": list(sample.completion_logprobs),
            "reward_score": sample.reward,
            "num_turns": 2,
            "metrics": {"generate_sequences": sample.model_seconds, "tool_calls": sample.env_seconds,
                        "compute_score": 0.0, "num_preempted": -1},
            "extra_fields": {"min_global_steps": sample.policy_version, "max_global_steps": sample.policy_version},
        }
        for sample in samples
    ]


def skyrl_generator_output(samples: Sequence[TurnSample]) -> dict[str, Any]:
    """Step wise: one row per turn, an episode's turns adjacent, the reward on its last turn only."""
    return {
        "prompt_token_ids": [list(sample.prompt_token_ids) for sample in samples],
        "response_ids": [list(sample.completion_token_ids) for sample in samples],
        "rewards": [sample.reward if sample.is_last_turn else 0.0 for sample in samples],
        "loss_masks": [[1] * len(sample.completion_token_ids) for sample in samples],
        "stop_reasons": None,
        "rollout_metrics": None,
        "rollout_logprobs": [list(sample.completion_logprobs) for sample in samples],
        "trajectory_ids": [{"instance_id": sample.group_id, "repetition_id": sample.sample_index}
                           for sample in samples],
        "is_last_step": [sample.is_last_turn for sample in samples],
    }


def prime_rl_training_samples(samples: Sequence[TurnSample], temperature: float) -> list[dict[str, Any]]:
    """Fields in TrainingSample wire order; masks, logprobs and advantages span the full sequence."""
    rows = []
    for sample in samples:
        prompt_length = len(sample.prompt_token_ids)
        completion_length = len(sample.completion_token_ids)
        rows.append({
            "token_ids": [*sample.prompt_token_ids, *sample.completion_token_ids],
            "mask": [False] * prompt_length + [True] * completion_length,
            "logprobs": [0.0] * prompt_length + list(sample.completion_logprobs),
            "temperatures": [temperature] * (prompt_length + completion_length),
            "env_name": sample.template,
            "advantages": [0.0] * prompt_length + [sample.advantage] * completion_length,
        })
    return rows


def areal_rows(samples: Sequence[TurnSample]) -> list[dict[str, Any]]:
    """Per token fields over prompt plus completion; versions are -1 where the policy sampled nothing."""
    rows = []
    for sample in samples:
        prompt_length = len(sample.prompt_token_ids)
        completion_length = len(sample.completion_token_ids)
        rows.append({
            "input_ids": [*sample.prompt_token_ids, *sample.completion_token_ids],
            "attention_mask": [True] * (prompt_length + completion_length),
            "loss_mask": [0] * prompt_length + [1] * completion_length,
            "logprobs": [0.0] * prompt_length + list(sample.completion_logprobs),
            "versions": [-1] * prompt_length + [sample.policy_version] * completion_length,
            "rewards": sample.reward,
        })
    return rows
