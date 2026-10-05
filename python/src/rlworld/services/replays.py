"""Fresh envs at a task's start state, where gates replay solutions (DESIGN.md section 6.10).

The authoring kit and gate reruns both lease an env of the task's snapshot,
inject the task setup and play a solution's actions before freezing input and
extracting the state the verifiers grade. A failing env becomes the attempt's
env error, which the gates report instead of crashing.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from rlworld.actions.models import ActionModel
from rlworld.rewards.records import EpisodeOutcome
from rlworld.rollout.episode import TaskInstance
from rlworld.rollout.groups import EnvShape
from rlworld.sdk.client import FleetError, LeaseGroupRequest, LeaseInfo, StepResult
from rlworld.specs.document import SpecDocument
from rlworld.tasks.gates import SolutionAttempt


class ReplayFleet(Protocol):
    def lease_group(self, request: LeaseGroupRequest) -> list[LeaseInfo]: ...

    def inject(self, lease_id: str, payload: dict[str, Any]) -> None: ...

    def step(self, lease_id: str, action: ActionModel) -> StepResult: ...

    def finish(self, lease_id: str, apps: list[str]) -> dict[str, Any]: ...

    def close(self, lease_id: str) -> None: ...


def playable(actions: Sequence[ActionModel]) -> tuple[list[ActionModel], EpisodeOutcome]:
    """The env actions to replay up to the first ending action with how that ending reads as an outcome."""
    steps: list[ActionModel] = []
    for action in actions:
        if action.ends_episode:
            return steps, EpisodeOutcome(steps_taken=len(steps), final_action=action.name,
                                         answer_text=getattr(action, "text", None))
        if action.modality != "control":
            steps.append(action)
    return steps, EpisodeOutcome(steps_taken=len(steps), final_action="done")


@dataclass(frozen=True)
class FreshEnvs:
    """Fresh envs of one task instance, leased under one run id, tenant and admission priority."""

    fleet: ReplayFleet
    shape: EnvShape
    document: SpecDocument
    instance: TaskInstance
    run_id: str
    tenant: str
    priority: str
    endpoint: str
    ttl_seconds: int

    def lease(self) -> LeaseInfo:
        """An env at the task's start state: the instance's snapshot restored at the world's clock, setup injected."""
        request = LeaseGroupRequest(
            run_id=self.run_id, tenant=self.tenant, pool=self.shape.pool, priority=self.priority,
            endpoint=self.endpoint, snapshot=self.instance.snapshot, memory_mib=self.shape.memory_mib,
            vcpus=self.shape.vcpus, cpu_millicores=self.shape.cpu_millicores, cost_center=self.endpoint,
            ttl_seconds=self.ttl_seconds, host_scoped_licenses=self.shape.host_scoped_licenses,
            world_time=self.document.world.clock.lease_time(),
            observation_kinds=tuple(self.instance.task.observation))
        lease = self.fleet.lease_group(request)[0]
        try:
            self.fleet.inject(lease.lease_id, self.instance.setup_payload)
        except Exception:
            self.close_quietly(lease.lease_id)
            raise
        return lease

    def close_quietly(self, lease_id: str) -> None:
        try:
            self.fleet.close(lease_id)
        except FleetError:
            return

    def final_state(self, lease_id: str) -> dict[str, Any]:
        """Freezes input, then extracts the state of every app the world materializes."""
        return self.fleet.finish(lease_id, list(self.document.world.materialize))

    def replay(self, actions: Sequence[ActionModel], name: str) -> SolutionAttempt:
        """Plays a solution in a fresh env; a failing env becomes an env error the gates report."""
        steps, outcome = playable(actions)
        try:
            lease = self.lease()
        except FleetError as error:
            return SolutionAttempt(name, {}, outcome.model_copy(update={"env_error": f"lease failed: {error}"}))
        try:
            for action in steps:
                self.fleet.step(lease.lease_id, action)
            final_state = self.final_state(lease.lease_id)
        except FleetError as error:
            return SolutionAttempt(name, {}, outcome.model_copy(update={"env_error": f"replay failed: {error}"}))
        finally:
            self.close_quietly(lease.lease_id)
        return SolutionAttempt(name, final_state, outcome)
