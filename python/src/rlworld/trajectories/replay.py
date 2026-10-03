"""Replay two ways (DESIGN.md section 6.11), plus rescoring.

Exact replay walks the recorded observations and actions. Rerunning the actions
from the start snapshot happens in an env; measure_divergence compares those
fresh observations with the recorded ones as a determinism health metric.
Rescoring runs today's verifiers on a past episode's recorded states.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from rlworld.actions.models import Action, Observation, parse_action
from rlworld.rewards.pipeline import JudgeTier, evaluate_episode
from rlworld.rewards.records import EpisodeOutcome, RewardPolicy, RewardRecord
from rlworld.rewards.verifiers import VerifierRegistry
from rlworld.specs.models import TaskSpec
from rlworld.trajectories.records import EpisodeRecord, StepRecord
from rlworld.trajectories.trajectory_store import TrajectoryStore

EPISODE_ENDING_ACTIONS = frozenset({"answer", "done", "give_up"})


@dataclass(frozen=True)
class ReplayFrame:
    step_index: int
    observation: Observation
    actions: tuple[Action, ...]
    model_output: str


@dataclass(frozen=True)
class DivergenceReport:
    compared_steps: int
    matching_steps: int
    first_divergent_step: int | None

    @property
    def divergence_rate(self) -> float:
        return 1 - self.matching_steps / self.compared_steps if self.compared_steps else 0.0


def replay_frames(episode: EpisodeRecord) -> Iterator[ReplayFrame]:
    for step in episode.steps:
        yield ReplayFrame(
            step_index=step.step_index,
            observation=step.observation,
            actions=tuple(parse_action(action_payload) for action_payload in step.actions),
            model_output=step.model_turn.output_text,
        )


def recorded_outcome(episode: EpisodeRecord) -> EpisodeOutcome:
    """Rebuild how the episode ended from its last step."""
    final_action = None
    answer_text = None
    agent_error = None
    if episode.steps:
        last_step = episode.steps[-1]
        agent_error = last_step.parse_error
        for action_payload in last_step.actions:
            if action_payload.get("name") in EPISODE_ENDING_ACTIONS:
                final_action = action_payload["name"]
                answer_text = action_payload.get("text") if final_action == "answer" else None
    return EpisodeOutcome(
        steps_taken=len(episode.steps), final_action=final_action, answer_text=answer_text, agent_error=agent_error
    )


def rescore_episode(
    trajectory_store: TrajectoryStore,
    episode: EpisodeRecord,
    task: TaskSpec,
    registry: VerifierRegistry,
    policy: RewardPolicy | None = None,
    judge: JudgeTier | None = None,
) -> RewardRecord:
    """Score a past episode with the verifiers registered today, for example after a verifier fix."""
    if episode.termination == "env_error":
        raise ValueError(f"episode {episode.episode_id} ended in an env error, so its states are incomplete")
    missing_refs = [
        name
        for name, ref in (("initial", episode.initial_state_ref), ("final", episode.final_state_ref),
                          ("ground truth", episode.ground_truth_ref))
        if ref is None
    ]
    if missing_refs:
        raise ValueError(f"episode {episode.episode_id} lacks recorded {', '.join(missing_refs)} state")
    return evaluate_episode(
        task,
        registry,
        trajectory_store.load_state(episode.initial_state_ref),
        trajectory_store.load_state(episode.final_state_ref),
        trajectory_store.load_state(episode.ground_truth_ref),
        recorded_outcome(episode),
        policy,
        judge,
        instruction=episode.instruction,
    )


def observation_fingerprint(observation: Observation) -> tuple[Any, ...]:
    screenshot_hash = observation.screenshot.sha256 if observation.screenshot else None
    accessibility_signature = (
        tuple((node.role, node.name, node.value) for node in observation.accessibility)
        if observation.accessibility is not None
        else None
    )
    terminal_signature = (
        (observation.terminal.stdout, observation.terminal.exit_code) if observation.terminal else None
    )
    return screenshot_hash, accessibility_signature, terminal_signature


def measure_divergence(recorded_steps: Sequence[StepRecord], rerun_observations: Sequence[Observation]) -> DivergenceReport:
    compared_steps = min(len(recorded_steps), len(rerun_observations))
    matching_steps = 0
    first_divergent_step = None
    for step, rerun_observation in zip(recorded_steps, rerun_observations):
        if observation_fingerprint(step.observation) == observation_fingerprint(rerun_observation):
            matching_steps += 1
        elif first_divergent_step is None:
            first_divergent_step = step.step_index
    if first_divergent_step is None and len(recorded_steps) != len(rerun_observations):
        first_divergent_step = compared_steps
    return DivergenceReport(compared_steps, matching_steps, first_divergent_step)
