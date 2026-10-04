"""Platform hosted rollout workers (DESIGN.md sections 5 and 6.7).

experimentd plays eval and data runs, dry runs and the solvability gate's
strong agent itself; RL runs import the same workers inside their trainer jobs.
Each worker takes its tasks from the registry, its policy client from the
gateway in the run's action format, a reward service with the run's judge when
the task has a rubric, the user simulator when the task asks for one, the
same simulator stack for colleagues who answer in chat and entitlements for
the image's licenses. It leases envs of the pool that serves the run's image.
Training runs screen their tasks, so none nearly duplicates a public benchmark.
"""

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from rlworld.actions.adapters import get_adapter
from rlworld.gateway.clients import ChatGateway, GatewayJudgeClient
from rlworld.gateway.metering import UsageMeter
from rlworld.judge.judge import RubricJudge
from rlworld.judge.prompt import JudgePromptBuilder
from rlworld.judge.rubric import RubricLibrary
from rlworld.rewards.pipeline import JudgeTier
from rlworld.rewards.records import EpisodeOutcome
from rlworld.rollout.episode import EntitlementSource, EpisodeRunner, EpisodeSettings, TaskInstance
from rlworld.rollout.groups import EnvShape, GroupRunner, LeaseSettings
from rlworld.rollout.policy import PolicyClient
from rlworld.rollout.user_simulator import GatewayNpcSimulator, GatewayUserSimulator
from rlworld.rollout.worker import RolloutWorker, RunControl
from rlworld.services.reward_service import RewardService
from rlworld.specs.document import SpecDocument
from rlworld.specs.models import EndpointSpec
from rlworld.tasks.gates import SolutionAttempt
from rlworld.tasks.registry import RegistryTaskProvider, SnapshotResolver, TaskRegistry, TaskScreen, WorldPreparer
from rlworld.trajectories.records import EpisodeRecord
from rlworld.trajectories.trajectory_store import TrajectoryStore

SOLVABILITY_PRIORITY = "eval"


class PlatformFleet(Protocol):
    """fleetd as workers use it: leases, steps, finishing and closing."""

    def lease_group(self, request: Any) -> list[Any]: ...

    def heartbeat(self, lease_id: str) -> Any: ...

    def step(self, lease_id: str, action: Any) -> Any: ...

    def finish(self, lease_id: str, apps: list[str]) -> dict[str, Any]: ...

    def close(self, lease_id: str) -> None: ...


@dataclass(frozen=True)
class SimulatorEndpoint:
    """The model that plays a task's user when the task asks for one."""

    endpoint: str
    model: str


class PlatformWorkers:
    """experimentd's WorkerFactory: one rollout worker per run, wired to the platform's gateway and stores.

    control is the experiment service, set once it exists, since it is built with these workers.
    """

    def __init__(self, fleet: PlatformFleet, gateway: ChatGateway, trajectory_store: TrajectoryStore,
                 registry: TaskRegistry, snapshots: SnapshotResolver, shapes: Mapping[str, EnvShape],
                 tenants: Callable[[str], str], judge_secret: bytes, rubric_library: RubricLibrary | None = None,
                 user_simulator: SimulatorEndpoint | None = None, entitlements: EntitlementSource | None = None,
                 meter: UsageMeter | None = None, control: RunControl | None = None,
                 worlds: WorldPreparer | None = None, screen: TaskScreen | None = None) -> None:
        self.fleet = fleet
        self.gateway = gateway
        self.trajectory_store = trajectory_store
        self.registry = registry
        self.snapshots = snapshots
        self.shapes = dict(shapes)
        self.tenants = tenants
        self.judge_secret = judge_secret
        self.rubric_library = rubric_library
        self.user_simulator = user_simulator
        self.entitlements = entitlements
        self.meter = meter
        self.control = control
        self.worlds = worlds
        self.screen = screen

    def judge(self, run_id: str, document: SpecDocument) -> JudgeTier | None:
        """The run's judge grades tasks with a rubric; the rest are graded by verifiers alone."""
        if document.run.judge is None or not document.task.judge_rubric or self.rubric_library is None:
            return None
        return RubricJudge(GatewayJudgeClient(self.gateway, document.run.judge.endpoint, run_id),
                           self.rubric_library, document.run.judge.model, JudgePromptBuilder(self.judge_secret))

    def group_runner(self, run_id: str, document: SpecDocument) -> GroupRunner:
        adapter = get_adapter(document.run.policy.action_format)
        geometry = adapter.model_geometry(document.image.display.width, document.image.display.height)
        policy = PolicyClient(self.gateway, document.run.policy, adapter, geometry, run_id, document.run.type)
        finisher = RewardService(self.fleet, self.registry.verifier_registry, judge=self.judge(run_id, document))
        simulator = colleagues = None
        if self.user_simulator is not None:
            colleagues = GatewayNpcSimulator(self.gateway, self.user_simulator.endpoint, self.user_simulator.model,
                                             run_id)
        if document.task.user_simulator is not None and self.user_simulator is not None:
            simulator = GatewayUserSimulator(self.gateway, self.user_simulator.endpoint, self.user_simulator.model,
                                             run_id)
        settings = EpisodeSettings(run_id, document.run.type, document.image.os, tuple(document.world.materialize),
                                   document.run.history)
        episodes = EpisodeRunner(self.fleet, policy, finisher, self.trajectory_store, settings, simulator,
                                 self.entitlements, npc_simulator=colleagues)
        lease = LeaseSettings(run_id=run_id, tenant=self.tenants(run_id), cost_center=document.experiment.name,
                              policy_endpoint=document.run.policy.endpoint,
                              shape=self.shapes[document.image.pool.vmm], licenses=tuple(document.image.licenses))
        return GroupRunner(self.fleet, episodes, lease, self.entitlements)

    def __call__(self, run_id: str, document: SpecDocument) -> RolloutWorker:
        provider = RegistryTaskProvider(self.registry, document, self.snapshots, worlds=self.worlds,
                                        screen=self.screen)
        return RolloutWorker(run_id, document, self.group_runner(run_id, document), provider, control=self.control,
                             meter=self.meter)


def episode_outcome(episode: EpisodeRecord, steps_taken: int) -> EpisodeOutcome:
    """How a played episode ended, in the terms the gates grade a final state by."""
    reward = episode.reward
    if reward is None or reward.termination == "env_error":
        detail = reward.error_detail if reward is not None else "the episode never finished"
        return EpisodeOutcome(steps_taken=steps_taken, env_error=detail or "env error")
    if reward.termination == "agent_error":
        return EpisodeOutcome(steps_taken=steps_taken, agent_error=reward.error_detail or "agent error")
    if reward.termination == "max_steps":
        return EpisodeOutcome(steps_taken=steps_taken)
    return EpisodeOutcome(steps_taken=steps_taken, final_action="done")


class PlatformSolvabilityProbe:
    """The solvability gate's strong agent (DESIGN.md 6.10): a strong model plays the task tries times, each try
    in a fresh env forked from the task start state; the gates grade the states it left."""

    def __init__(self, workers: PlatformWorkers, agent: EndpointSpec) -> None:
        self.workers = workers
        self.agent = agent
        self.run_numbers = itertools.count(1)

    def attempts(self, document: SpecDocument, instance: TaskInstance, tries: int) -> list[SolutionAttempt]:
        run_id = f"solvability_{next(self.run_numbers):06d}"
        run = document.run.model_copy(update={"type": "eval", "policy": self.agent, "group_size": tries})
        runner = self.workers.group_runner(run_id, document.model_copy(update={"run": run}))
        result = runner.run(instance, tries, 0, f"{run_id}_g00000", SOLVABILITY_PRIORITY)
        attempts = []
        for index, episode in enumerate(result.episodes):
            store = self.workers.trajectory_store
            final_state = store.load_state(episode.final_state_ref) if episode.final_state_ref else {}
            steps_taken = len(store.step_keys(run_id, episode.episode_id))
            attempts.append(SolutionAttempt(f"strong_agent_{index}", final_state,
                                            episode_outcome(episode, steps_taken)))
        return attempts
