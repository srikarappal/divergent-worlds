"""The task registry and the task provider rollout workers draw from (DESIGN.md sections 5 and 6.10).

The registry holds task templates under the dotted names task specs use for
their generator and oracle and registers the verifiers the templates ship. A
run's provider builds the run's world once and keeps a stable list of tasks at
the spec's difficulty: the eval suite, or the starting pool of an RL run that
then grows wherever the curriculum planner finds the learnable band or steps
difficulty up or down. Each task's instance is generated on first use: the
world task setup materializes, the start state that materialization extracts
to, the ground truth that stays outside the VM and the snapshot of the run's
image.
"""

import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rlworld.actions.models import ActionModel
from rlworld.rewards.verifiers import UnknownVerifierError, VerifierRegistry
from rlworld.rollout.episode import TaskInstance
from rlworld.sdk.client import SnapshotSource
from rlworld.specs.digest import spec_digest
from rlworld.specs.document import SpecDocument
from rlworld.specs.models import DifficultySpec, ImageSpec
from rlworld.tasks.curriculum import CandidateTask, CurriculumPlanner, PassRateTracker, difficulty_key
from rlworld.tasks.finance import ReconcileInvoices
from rlworld.tasks.generators import (
    GeneratedTask,
    TaskGenerationError,
    TaskTemplate,
    generated_task_digest,
    task_identifier,
    task_seed,
)
from rlworld.worlds.generator import build_world
from rlworld.worlds.integrations import extract_world_state, materialize_world
from rlworld.worlds.model import World, world_digest


class UnknownTaskTemplateError(KeyError):
    """A task spec names a generator or oracle no template registered."""


class UnknownSnapshotError(KeyError):
    """No snapshot of the run's image is known yet."""


class SnapshotResolver(Protocol):
    def snapshot(self, image: ImageSpec) -> SnapshotSource: ...


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
    """What task setup injects after restore: the task's world into its apps, then the windows it opens."""
    payload: dict[str, Any] = {"world": task.world.model_dump(mode="json"), "apps": list(apps)}
    if task.open_windows:
        payload["open_windows"] = list(task.open_windows)
    return payload


@dataclass(frozen=True)
class RegisteredTask:
    candidate: CandidateTask
    seed: int


class RegistryTaskProvider:
    """One run's view of the registry; safe to call from the worker's group threads."""

    def __init__(self, registry: TaskRegistry, document: SpecDocument, snapshots: SnapshotResolver,
                 max_tasks_per_difficulty: int = 256) -> None:
        problems = registry.problems(document)
        if problems:
            raise TaskGenerationError("; ".join(problems))
        self.document = document
        self.template = registry.template(document.task.generator)
        self.max_tasks_per_difficulty = max_tasks_per_difficulty
        self.world = build_world(document.world)
        self.world_digest = world_digest(self.world)
        self.apps = tuple(document.world.materialize)
        self.snapshot = snapshots.snapshot(document.image)
        self.lock = threading.Lock()
        self.tasks: dict[str, RegisteredTask] = {}
        self.tasks_by_difficulty: dict[str, int] = {}
        self.instances: dict[str, TaskInstance] = {}
        self.generated: dict[str, GeneratedTask] = {}
        run = document.run
        self.add_tasks(document.task.difficulty, run.tasks_per_batch * (1 if run.type == "rl" else run.batches))
        self.instance(next(iter(self.tasks.values())).candidate)

    def add_tasks(self, difficulty: DifficultySpec, count: int) -> None:
        """New seeds continue where the difficulty's earlier tasks stopped, so ids never repeat or shift."""
        key = difficulty_key(difficulty)
        start = self.tasks_by_difficulty.get(key, 0)
        stop = min(start + count, self.max_tasks_per_difficulty)
        for index in range(start, stop):
            seed = task_seed(self.document.world.seed, self.template.name, difficulty, index)
            task_id = task_identifier(self.template, difficulty, self.world_digest, seed)
            self.tasks.setdefault(task_id, RegisteredTask(CandidateTask(task_id, self.template.name, difficulty),
                                                          seed))
        self.tasks_by_difficulty[key] = max(start, stop)

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

    def instance(self, candidate: CandidateTask) -> TaskInstance:
        with self.lock:
            cached = self.instances.get(candidate.task_id)
            registered = self.tasks[candidate.task_id]
        if cached is not None:
            return cached
        difficulty = registered.candidate.difficulty
        generated = self.generated_task(candidate)
        instance = TaskInstance(
            task_id=candidate.task_id,
            template=self.template.name,
            task=self.document.task.model_copy(update={"difficulty": difficulty}),
            task_digest=generated_task_digest(self.template, generated),
            world_digest=self.world_digest,
            seed=registered.seed,
            instruction=generated.instruction,
            snapshot=self.snapshot,
            initial_state=start_state(generated.world, self.apps),
            ground_truth=generated.ground_truth,
            setup_payload=setup_payload(generated, self.apps),
            hidden_intent=generated.hidden_intent,
            personas=generated.personas,
            secrets=generated.secrets,
        )
        with self.lock:
            return self.instances.setdefault(candidate.task_id, instance)
