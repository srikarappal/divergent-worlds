"""Validation gate reports per task and the recipes that rerun them (DESIGN.md sections 6.2 and 6.10).

Preflight reads a task's latest report by the digest of its task spec. Each
report keeps the recipe that produced it with the image, pool and snapshot it
ran on, so an image bump reruns exactly those gates on the new snapshot.
"""

import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, NonNegativeInt

from rlworld.tasks.gates import GateReport


class GateRecipe(BaseModel):
    """What a task's gates replay: the authored task and the solutions recorded for it, as canonical actions."""

    spec_digest: str
    task_index: NonNegativeInt
    # A recorded oracle; None replays the template's scripted oracle.
    oracle: list[dict[str, Any]] | None = None
    alternatives: list[list[dict[str, Any]]] = Field(default_factory=list)
    near_misses: list[list[dict[str, Any]]] = Field(default_factory=list)


@dataclass(frozen=True)
class GateRecord:
    task_spec_digest: str
    template: str
    task_id: str
    image: str
    image_digest: str
    pool: str
    snapshot: str
    report: GateReport
    recipe: GateRecipe
    ran_at: float
    source: Literal["authoring", "image_bump"]


class GateReports(Mapping[str, GateReport]):
    """The latest gate report of every task, which preflight reads as a mapping by task spec digest."""

    def __init__(self) -> None:
        self.records: dict[str, GateRecord] = {}
        self.lock = threading.Lock()

    def record(self, record: GateRecord) -> None:
        with self.lock:
            self.records[record.task_spec_digest] = record

    def latest(self) -> list[GateRecord]:
        with self.lock:
            return list(self.records.values())

    def __getitem__(self, task_spec_digest: str) -> GateReport:
        with self.lock:
            return self.records[task_spec_digest].report

    def __iter__(self) -> Iterator[str]:
        with self.lock:
            return iter(list(self.records))

    def __len__(self) -> int:
        with self.lock:
            return len(self.records)
