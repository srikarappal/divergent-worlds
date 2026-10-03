"""Gymnasium style env over one fleet lease (DESIGN.md section 6.3).

reset returns (observation, info); step returns (observation, reward, terminated,
truncated, info). An episode ending action or the step budget freezes input and
extracts app state; the reward function, usually the reward service, scores it.
"""

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from rlworld.actions.models import ActionModel, Observation, ensure_actions_allowed
from rlworld.rewards.records import EpisodeOutcome
from rlworld.sdk.client import LeaseInfo, StepResult


class FleetApi(Protocol):
    def reset(self, lease_id: str) -> StepResult: ...

    def step(self, lease_id: str, action: ActionModel) -> StepResult: ...

    def finish(self, lease_id: str, apps: list[str]) -> dict[str, Any]: ...

    def close(self, lease_id: str) -> None: ...


RewardFunction = Callable[[dict[str, Any], EpisodeOutcome], float]


class ComputerUseEnv:
    def __init__(
        self,
        client: FleetApi,
        lease: LeaseInfo,
        allowed_modalities: Sequence[str],
        max_steps: int,
        apps_to_extract: Sequence[str],
        reward_function: RewardFunction | None = None,
    ) -> None:
        self.client = client
        self.lease = lease
        self.allowed_modalities = tuple(allowed_modalities)
        self.max_steps = max_steps
        self.apps_to_extract = list(apps_to_extract)
        self.reward_function = reward_function
        self.steps_taken = 0
        self.finished = False
        self.last_observation: Observation | dict[str, Any] | None = None

    def reset(self) -> tuple[Observation | dict[str, Any], dict[str, Any]]:
        result = self.client.reset(self.lease.lease_id)
        self.steps_taken = 0
        self.finished = False
        self.last_observation = result.observation
        return result.observation, {"env_seconds": result.env_seconds}

    def finish_episode(self, outcome: EpisodeOutcome) -> tuple[float, dict[str, Any]]:
        final_state = self.client.finish(self.lease.lease_id, self.apps_to_extract)
        self.finished = True
        reward = self.reward_function(final_state, outcome) if self.reward_function else 0.0
        return reward, {"final_state": final_state, "outcome": outcome}

    def step(self, action: ActionModel) -> tuple[Observation | dict[str, Any] | None, float, bool, bool,
                                                 dict[str, Any]]:
        if self.finished:
            raise RuntimeError("the episode already finished; call reset or close")
        ensure_actions_allowed([action], self.allowed_modalities)
        if action.ends_episode:
            outcome = EpisodeOutcome(steps_taken=self.steps_taken, final_action=action.name,
                                     answer_text=getattr(action, "text", None))
            reward, info = self.finish_episode(outcome)
            return self.last_observation, reward, True, False, info
        result = self.client.step(self.lease.lease_id, action)
        self.steps_taken += 1
        self.last_observation = result.observation
        info: dict[str, Any] = {"env_seconds": result.env_seconds, "settle_seconds": result.settle_seconds,
                                "settle_capped": result.settle_capped}
        if self.steps_taken >= self.max_steps:
            reward, finish_info = self.finish_episode(EpisodeOutcome(steps_taken=self.steps_taken))
            return result.observation, reward, False, True, {**info, **finish_info}
        return result.observation, 0.0, False, False, info

    def close(self) -> None:
        self.client.close(self.lease.lease_id)
