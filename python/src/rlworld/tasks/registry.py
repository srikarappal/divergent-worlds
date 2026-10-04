"""The task registry and the task provider rollout workers draw from (DESIGN.md sections 5 and 6.10).

The registry holds task templates under the dotted names task specs use for
their generator and oracle and registers the verifiers the templates ship. A
run's provider builds the run's world once and keeps a stable list of tasks at
the spec's difficulty: the eval suite, or the starting pool of an RL run that
then grows wherever the curriculum planner finds the learnable band or steps
difficulty up or down. Each task's instance is generated on first use: the
world task setup materializes, the start state that materialization extracts
to, the ground truth that stays outside the VM and the snapshot of the run's
image. When fleetd gave the run's world a world snapshot, because it loads
slowly or differently each time, instances restore from it and inject only
what their task changes. A task suite, such as an imported public benchmark,
hands out its fixed tasks in order and runs only as eval, since training must
not overlap public benchmarks. For the same reason a training run's provider
screens every task it generates and never registers one its screen blocks,
such as a near duplicate of a public benchmark task. Instances also carry the
scripted events of their world that land in the run's apps and the colleagues
who answer in chat.
"""

import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from rlworld.actions.models import ActionModel
from rlworld.rewards.verifiers import UnknownVerifierError, VerifierRegistry
from rlworld.rollout.episode import TaskInstance
from rlworld.rollout.groups import EnvShape
from rlworld.rollout.world_events import NonPlayer, ScheduledDelivery
from rlworld.sdk.client import SnapshotNotFoundError, SnapshotSource
from rlworld.specs.digest import spec_digest
from rlworld.specs.document import SpecDocument
from rlworld.specs.models import DifficultySpec, ImageSpec
from rlworld.tasks.curriculum import CandidateTask, CurriculumPlanner, PassRateTracker, difficulty_key
from rlworld.tasks.finance import ReconcileInvoices
from rlworld.tasks.generators import (
    GeneratedTask,
    NpcSpec,
    TaskGenerationError,
    TaskSuite,
    TaskTemplate,
    generated_task_digest,
    task_identifier,
    task_seed,
)
from rlworld.worlds.delta import world_delta
from rlworld.worlds.generator import build_world
from rlworld.worlds.integrations import extract_world_state, materialize_world
from rlworld.worlds.model import World, world_digest
from rlworld.worlds.perturbations import event_record


class UnknownTaskTemplateError(KeyError):
    """A task spec names a generator or oracle no template registered."""


class UnknownSnapshotError(KeyError):
    """No snapshot of the run's image is known yet."""


class SnapshotResolver(Protocol):
    def snapshot(self, image: ImageSpec) -> SnapshotSource: ...


class SnapshotIndex(Protocol):
    def current_snapshot(self, image_digest: str, pool: str) -> dict[str, Any]: ...


class FleetSnapshots:
    """The snapshot new leases of an image use on its pool, from the fleet's snapshot store (DESIGN.md 6.2).

    Leases name it by digest alone; the host pulls its files.
    """

    def __init__(self, fleet: SnapshotIndex, pools: Mapping[str, str]) -> None:
        self.fleet = fleet
        self.pools = dict(pools)

    def snapshot(self, image: ImageSpec) -> SnapshotSource:
        pool = self.pools.get(image.pool.vmm)
        if pool is None:
            raise UnknownSnapshotError(f"no pool runs {image.pool.vmm} images")
        try:
            current = self.fleet.current_snapshot(spec_digest(image), pool)
        except SnapshotNotFoundError as error:
            raise UnknownSnapshotError(f"no snapshot of image {image.id} on {pool} yet") from error
        return SnapshotSource(digest=current["snapshot"], state_path="", memory_path="", base_disk_path="")


class WorldIndex(Protocol):
    def prepare_world(self, base_digest: str, world_digest: str, payload: dict[str, Any], apps: list[str], pool: str,
                      memory_mib: int, cpu_millicores: int, vcpus: int) -> dict[str, Any]: ...


class TaskScreen(Protocol):
    def blocked(self, task: GeneratedTask) -> str | None: ...


class WorldPreparer(Protocol):
    def prepare(self, base: SnapshotSource, world: World, apps: tuple[str, ...],
                image: ImageSpec) -> SnapshotSource | None: ...


class FleetWorlds:
    """fleetd decides once per base snapshot and world whether the world gets a world snapshot (DESIGN.md 3)."""

    def __init__(self, fleet: WorldIndex, shapes: Mapping[str, EnvShape]) -> None:
        self.fleet = fleet
        self.shapes = dict(shapes)

    def prepare(self, base: SnapshotSource, world: World, apps: tuple[str, ...],
                image: ImageSpec) -> SnapshotSource | None:
        """The world snapshot to restore from, or None when the world loads at every restore."""
        shape = self.shapes[image.pool.vmm]
        decision = self.fleet.prepare_world(base.digest, world_digest(world),
                                            {"world": world.model_dump(mode="json"), "apps": list(apps)}, list(apps),
                                            shape.pool, shape.memory_mib, shape.cpu_millicores, shape.vcpus)
        if not decision.get("snapshot"):
            return None
        return SnapshotSource(digest=decision["snapshot"], state_path="", memory_path="", base_disk_path="")


class StaticSnapshots:
    """Snapshots by image digest from configuration, for hosts the snapshot store does not serve."""

    def __init__(self, snapshots: Mapping[str, SnapshotSource]) -> None:
        self.snapshots = dict(snapshots)

    def snapshot(self, image: ImageSpec) -> SnapshotSource:
        image_digest = spec_digest(image)
        if image_digest not in self.snapshots:
            raise UnknownSnapshotError(f"no snapshot of image {image.id} ({image_digest})")
        return self.snapshots[image_digest]


class TaskRegistry:
    def __init__(self, verifier_registry: VerifierRegistry | None = None) -> None:
        self.verifier_registry = verifier_registry or VerifierRegistry()
        self.templates: dict[str, TaskTemplate] = {}
        self.oracles: dict[str, TaskTemplate] = {}

    def register(self, template: TaskTemplate) -> TaskTemplate:
        for verifier in template.verifiers():
            self.verifier_registry.register(verifier)
        self.templates[template.name] = template
        self.oracles[template.oracle_name] = template
        return template

    def template(self, name: str) -> TaskTemplate:
        if name not in self.templates:
            raise UnknownTaskTemplateError(f"task generator {name} is not registered")
        return self.templates[name]

    def problems(self, document: SpecDocument) -> list[str]:
        """Everything about a spec's task layer the registry cannot serve."""
        task = document.task
        if task.generator not in self.templates:
            return [f"task generator {task.generator} is not registered"]
        template = self.templates[task.generator]
        problems = []
        if task.oracle != template.oracle_name:
            problems.append(f"oracle {task.oracle} is not {task.generator}'s, which is {template.oracle_name}")
        if document.world.generator not in template.world_generators:
            problems.append(f"{task.generator} does not generate over {document.world.generator} worlds")
        if isinstance(template, TaskSuite) and (document.run.type != "eval" or task.split != "eval"):
            problems.append(f"{task.generator} is a public suite and runs only as eval on the eval split")
        if isinstance(template, TaskSuite) and template.size == 0:
            problems.append(f"{task.generator} has no tasks")
        for tier, names in (("exact", task.verifiers.exact), ("semantic", task.verifiers.semantic)):
            try:
                self.verifier_registry.resolve(names, tier)
            except (UnknownVerifierError, ValueError) as error:
                problems.append(f"{tier} verifiers: {error}")
        return problems


def default_task_registry(verifier_registry: VerifierRegistry | None = None) -> TaskRegistry:
    registry = TaskRegistry(verifier_registry)
    registry.register(ReconcileInvoices())
    return registry


def start_state(world: World, apps: tuple[str, ...]) -> dict[str, Any]:
    """The state a guest extracts right after task setup, from the same integrations, so diffs start clean."""
    with tempfile.TemporaryDirectory() as directory:
        materialize_world(world, apps, Path(directory))
        return extract_world_state(apps, Path(directory))


def setup_payload(task: GeneratedTask, apps: tuple[str, ...]) -> dict[str, Any]:
    """What task setup injects after restore: the task's world into its apps, the windows it opens, its setup
    steps and the read plan its verifiers need."""
    payload: dict[str, Any] = {"world": task.world.model_dump(mode="json"), "apps": list(apps)}
    if task.open_windows:
        payload["open_windows"] = list(task.open_windows)
    if task.setup:
        payload["setup"] = list(task.setup)
    if task.reads is not None:
        payload["reads"] = task.reads
    return payload


def scheduled_events(world: World, apps: tuple[str, ...]) -> tuple[ScheduledDelivery, ...]:
    """The world's scripted events that land in an app the run materializes."""
    return tuple(ScheduledDelivery(event.trigger_step, event.app, event_record(event, world), f"world event {event.id}")
                 for event in world.events if event.app in apps)


def non_players(world: World, npcs: tuple[NpcSpec, ...]) -> tuple[NonPlayer, ...]:
    """Colleagues as the simulator plays them: who they are in the world and what the task lets them know."""
    people = {person.id: person for person in world.people if person.id != world.agent_persona_id}
    players = []
    for npc in npcs:
        person = people.get(npc.person_id)
        if person is None:
            raise TaskGenerationError(f"colleague {npc.person_id} is nobody else in the world")
        persona = f"{person.full_name}, {person.role} in {person.department} at {world.organization_name}"
        players.append(NonPlayer(person.id, person.email.split("@")[0], persona, npc.knowledge))
    return tuple(players)


@dataclass(frozen=True)
class RegisteredTask:
    candidate: CandidateTask
    seed: int


class RegistryTaskProvider:
    """One run's view of the registry; safe to call from the worker's group threads."""

    def __init__(self, registry: TaskRegistry, document: SpecDocument, snapshots: SnapshotResolver,
                 max_tasks_per_difficulty: int = 256, worlds: WorldPreparer | None = None,
                 screen: TaskScreen | None = None) -> None:
        problems = registry.problems(document)
        if problems:
            raise TaskGenerationError("; ".join(problems))
        self.document = document
        self.template = registry.template(document.task.generator)
        self.suite = isinstance(self.template, TaskSuite)
        self.max_tasks_per_difficulty = self.template.size if self.suite else max_tasks_per_difficulty
        self.world = build_world(document.world)
        self.world_digest = world_digest(self.world)
        self.apps = tuple(document.world.materialize)
        self.snapshot = snapshots.snapshot(document.image)
        self.world_snapshot = (worlds.prepare(self.snapshot, self.world, self.apps, document.image)
                               if worlds is not None else None)
        self.lock = threading.Lock()
        self.tasks: dict[str, RegisteredTask] = {}
        self.tasks_by_difficulty: dict[str, int] = {}
        self.instances: dict[str, TaskInstance] = {}
        self.generated: dict[str, GeneratedTask] = {}
        self.screen = screen if document.task.split == "train" else None
        self.blocked: dict[str, str] = {}
        run = document.run
        self.add_tasks(document.task.difficulty, run.tasks_per_batch * (1 if run.type == "rl" else run.batches))
        if not self.tasks:
            reasons = "; ".join(sorted(set(self.blocked.values()))[:3])
            raise TaskGenerationError(f"every task this run would train on is blocked: {reasons}")
        self.instance(next(iter(self.tasks.values())).candidate)

    def add_tasks(self, difficulty: DifficultySpec, count: int) -> None:
        """New seeds continue where the difficulty's earlier tasks stopped, so ids never repeat or shift; a suite's
        seeds are its task indexes. A blocked task is skipped and the next seed takes its place."""
        key = difficulty_key(difficulty)
        index = self.tasks_by_difficulty.get(key, 0)
        added = 0
        while added < count and index < self.max_tasks_per_difficulty:
            seed = index if self.suite else task_seed(self.document.world.seed, self.template.name, difficulty, index)
            task_id = task_identifier(self.template, difficulty, self.world_digest, seed)
            index += 1
            if self.screened_out(task_id, difficulty, seed):
                continue
            self.tasks.setdefault(task_id, RegisteredTask(CandidateTask(task_id, self.template.name, difficulty),
                                                          seed))
            added += 1
        self.tasks_by_difficulty[key] = index

    def screened_out(self, task_id: str, difficulty: DifficultySpec, seed: int) -> bool:
        """Training tasks are generated as they are added so the screen sees their instruction."""
        if self.screen is None:
            return False
        if task_id in self.blocked:
            return True
        generated = self.generated.get(task_id) or self.template.generate(self.world, difficulty, seed)
        reason = self.screen.blocked(generated)
        if reason is not None:
            self.blocked[task_id] = reason
            return True
        self.generated[task_id] = generated
        return False

    def candidates(self) -> list[CandidateTask]:
        with self.lock:
            return [registered.candidate for registered in self.tasks.values()]

    def expand(self, tracker: PassRateTracker, policy_version: int) -> None:
        """More tasks wherever the curriculum finds the learnable band; ambiguity only when a user simulator answers."""
        with self.lock:
            difficulties = list({registered.candidate.difficulty.model_dump_json(): registered.candidate.difficulty
                                 for registered in self.tasks.values()}.values())
        requests = CurriculumPlanner(tracker).plan({self.template.name: difficulties}, policy_version)
        with self.lock:
            for request in requests:
                if request.difficulty.ambiguity != "none" and self.document.task.user_simulator is None:
                    continue
                self.add_tasks(request.difficulty, request.count)

    def generated_task(self, candidate: CandidateTask) -> GeneratedTask:
        with self.lock:
            cached = self.generated.get(candidate.task_id)
            registered = self.tasks[candidate.task_id]
        if cached is not None:
            return cached
        generated = self.template.generate(self.world, registered.candidate.difficulty, registered.seed)
        with self.lock:
            return self.generated.setdefault(candidate.task_id, generated)

    def oracle(self, candidate: CandidateTask) -> list[ActionModel]:
        """The template's scripted oracle for this task, the solution gates replay in a fresh env."""
        return self.template.oracle(self.generated_task(candidate))

    def start_from(self, generated: GeneratedTask) -> tuple[SnapshotSource, dict[str, Any]]:
        """The world snapshot with the task's delta when there is one and the task fits it, else the base with all."""
        if self.world_snapshot is not None:
            delta = world_delta(self.world, generated.world)
            if delta is not None:
                return self.world_snapshot, setup_payload(replace(generated, world=delta), self.apps)
        return self.snapshot, setup_payload(generated, self.apps)

    def instance(self, candidate: CandidateTask) -> TaskInstance:
        with self.lock:
            cached = self.instances.get(candidate.task_id)
            registered = self.tasks[candidate.task_id]
        if cached is not None:
            return cached
        difficulty = registered.candidate.difficulty
        generated = self.generated_task(candidate)
        snapshot, setup = self.start_from(generated)
        instance = TaskInstance(
            task_id=candidate.task_id,
            template=self.template.name,
            task=self.document.task.model_copy(update={"difficulty": difficulty}),
            task_digest=generated_task_digest(self.template, generated),
            world_digest=self.world_digest,
            seed=registered.seed,
            instruction=generated.instruction,
            snapshot=snapshot,
            initial_state=start_state(generated.world, self.apps),
            ground_truth=generated.ground_truth,
            setup_payload=setup,
            hidden_intent=generated.hidden_intent,
            personas=generated.personas,
            secrets=generated.secrets,
            world_events=scheduled_events(generated.world, self.apps),
            npcs=non_players(generated.world, generated.npcs),
        )
        with self.lock:
            return self.instances.setdefault(candidate.task_id, instance)
