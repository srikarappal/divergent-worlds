"""Task templates generated over worlds along the difficulty axes (DESIGN.md sections 3 and 6.10).

A template turns a world, a difficulty and a seed into one task: the
instruction, the world as the task sees it with its perturbations, the ground
truth that stays outside the VM, the hidden intent a user simulator answers
from and the windows task setup opens. It also writes the task's scripted
oracle in canonical actions and ships the verifiers its task specs name. The
same template, world, difficulty and seed always give the same task. A task
may name colleagues who answer the agent in chat with what they know.

A task suite is a fixed list of tasks, such as an imported public benchmark:
its index-th task is generated with seed index and it runs only as eval.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from rlworld.actions.models import ActionModel
from rlworld.rewards.verifiers import Verifier
from rlworld.specs.digest import canonical_json, content_digest
from rlworld.specs.models import DifficultySpec
from rlworld.tasks.curriculum import difficulty_key
from rlworld.worlds.model import World, world_digest


class TaskGenerationError(ValueError):
    """A template cannot make a task for this world or difficulty."""


@dataclass(frozen=True)
class NpcSpec:
    """A colleague of the world who answers the agent in chat; what they know stays on the host like ground truth."""

    person_id: str
    knowledge: str


@dataclass(frozen=True)
class GeneratedTask:
    """ground_truth is plain JSON and never enters the VM; the world is materialized at task setup.

    setup holds ordered guest setup steps after the world and windows; reads is the plan of files and command
    outputs the guest hands back with the final state for the verifiers.
    """

    instruction: str
    world: World
    ground_truth: dict[str, Any]
    hidden_intent: str = ""
    open_windows: tuple[str, ...] = ()
    personas: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    setup: tuple[dict[str, Any], ...] = ()
    reads: dict[str, Any] | None = None
    npcs: tuple[NpcSpec, ...] = ()


class TaskTemplate(Protocol):
    name: str
    version: str
    oracle_name: str
    description: str
    world_generators: frozenset[str]

    def generate(self, world: World, difficulty: DifficultySpec, seed: int) -> GeneratedTask: ...

    def oracle(self, task: GeneratedTask) -> list[ActionModel]: ...

    def verifiers(self) -> Sequence[Verifier]: ...


@runtime_checkable
class TaskSuite(TaskTemplate, Protocol):
    """A fixed list of size tasks; seed i generates task i."""

    size: int


def task_seed(world_seed: int, template: str, difficulty: DifficultySpec, index: int) -> int:
    """The seed of a template's index-th task at one difficulty over one world, stable across processes."""
    material = f"{world_seed}:{template}:{difficulty_key(difficulty)}:{index}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big") >> 1


def task_identifier(template: TaskTemplate, difficulty: DifficultySpec, base_world_digest: str, seed: int) -> str:
    material = canonical_json({"template": template.name, "version": template.version,
                               "difficulty": difficulty.model_dump(mode="json"), "world": base_world_digest,
                               "seed": seed})
    return f"task_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def generated_task_digest(template: TaskTemplate, task: GeneratedTask) -> str:
    """The task version: what the agent faces, what it is graded against and which template made it.

    Setup steps, a read plan and colleagues count only when a task has them, so tasks without them keep their
    digests.
    """
    material = {"template": template.name, "version": template.version, "instruction": task.instruction,
                "world": world_digest(task.world), "ground_truth": task.ground_truth,
                "hidden_intent": task.hidden_intent, "open_windows": list(task.open_windows)}
    if task.setup:
        material["setup"] = list(task.setup)
    if task.reads is not None:
        material["reads"] = task.reads
    if task.npcs:
        material["npcs"] = [{"person_id": npc.person_id, "knowledge": npc.knowledge} for npc in task.npcs]
    return content_digest(material)
