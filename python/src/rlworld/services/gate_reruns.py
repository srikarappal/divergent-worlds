"""Reruns of validation gates when an image bump changes the snapshot a task runs on (DESIGN.md 6.2.6).

experimentd polls the fleet's snapshot index. When the newest snapshot of an
image on a pool differs from the one a task's latest gate report ran on, as a
new image version or a rebuild wave for a VMM upgrade makes it, the task's
gates rerun there: its spec with that image, the recorded or scripted oracle
replayed in fresh envs, recorded alternatives and near misses replayed too and
the strong agent asked when one is configured. The new report replaces the old
one for preflight and a failing one goes to human review. The images catalog
follows the index with each image's current snapshot per pool.
"""

import itertools
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from rlworld.actions.models import ActionModel, parse_action
from rlworld.rewards.pipeline import JudgeTier
from rlworld.rollout.groups import EnvShape
from rlworld.sdk.client import SnapshotSource
from rlworld.services.auth import DEFAULT_TENANT
from rlworld.services.authoring import SolvabilityProbe
from rlworld.services.catalogs import CatalogEntry, CatalogService
from rlworld.services.experiments import ExperimentService
from rlworld.services.gate_reports import GateRecipe, GateRecord, GateReports
from rlworld.services.replays import FreshEnvs, ReplayFleet
from rlworld.services.review import FiledItem, ReviewService
from rlworld.specs.digest import spec_digest
from rlworld.specs.document import SpecDocument
from rlworld.specs.models import ImageSpec
from rlworld.tasks.curriculum import CandidateTask
from rlworld.tasks.gates import GateConfig, GateInputs, TaskGateRunner
from rlworld.tasks.registry import RegistryTaskProvider, TaskRegistry

GATE_RERUN_PRIORITY = "eval"
GATE_RERUN_ENDPOINT = "gates"
GATE_RERUN_TTL_SECONDS = 3600
RERUN_ACTOR = "gate_reruns"


class RerunFleet(ReplayFleet, Protocol):
    def current_snapshots(self) -> list[dict[str, Any]]: ...

    def snapshot(self, digest: str) -> dict[str, Any]: ...


class FixedSnapshot:
    """Resolves every image to the one snapshot a rerun runs on."""

    def __init__(self, digest: str) -> None:
        self.digest = digest

    def snapshot(self, image: ImageSpec) -> SnapshotSource:
        return SnapshotSource(digest=self.digest, state_path="", memory_path="", base_disk_path="")


def updated_at(current: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(current["updated_at"])


def newest_snapshots(currents: Sequence[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """The newest current snapshot of each image id on each pool, so a new image version counts as a bump too."""
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for current in currents:
        key = (current["image"], current["pool"])
        if key not in newest or updated_at(current) > updated_at(newest[key]):
            newest[key] = current
    return newest


def parsed(actions: Sequence[dict[str, Any]]) -> list[ActionModel]:
    return [parse_action(payload) for payload in actions]


class GateReruns:
    def __init__(self, experiment_service: ExperimentService, registry: TaskRegistry, reports: GateReports,
                 fleet: RerunFleet, review_service: ReviewService, shapes: Mapping[str, EnvShape],
                 catalog_service: CatalogService | None = None, judge: JudgeTier | None = None,
                 solvability: SolvabilityProbe | None = None, gate_config: GateConfig | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.experiment_service = experiment_service
        self.registry = registry
        self.reports = reports
        self.fleet = fleet
        self.review_service = review_service
        self.shapes = dict(shapes)
        self.catalog_service = catalog_service
        self.judge = judge
        self.solvability = solvability
        self.gate_config = gate_config or GateConfig()
        self.clock = clock
        self.run_numbers = itertools.count(1)
        self.failed: dict[tuple[str, str], str] = {}
        self.lock = threading.Lock()

    def watch(self, stop: threading.Event, interval_seconds: float) -> None:
        """Polls until stopped; experimentd runs this on its own thread."""
        while not stop.wait(interval_seconds):
            self.poll()

    def poll(self) -> list[GateRecord]:
        """Reruns the gates of every task whose image has a newer snapshot; returns the new records."""
        newest = newest_snapshots(self.fleet.current_snapshots())
        if self.catalog_service is not None:
            self.publish_images(newest)
        rerun = []
        for record in self.reports.latest():
            current = newest.get((record.image, record.pool))
            if current is None or current["snapshot"] == record.snapshot:
                continue
            with self.lock:
                if (record.task_spec_digest, current["snapshot"]) in self.failed:
                    continue
            try:
                rerun.append(self.rerun(record, current))
            except Exception as error:  # one task that cannot rerun never stops the others
                self.fail(record, current, error)
        return rerun

    def document_on(self, record: GateRecord, current: dict[str, Any]) -> SpecDocument:
        """The task's spec with the image the new snapshot was built from, when a new image version replaced it."""
        document = self.experiment_service.spec(record.recipe.spec_digest)[1]
        if current["image_digest"] == spec_digest(document.image):
            return document
        image_spec = self.fleet.snapshot(current["snapshot"])["manifest"]["image_spec"]
        return document.model_copy(update={"image": ImageSpec.model_validate(image_spec)})

    def rerun(self, record: GateRecord, current: dict[str, Any]) -> GateRecord:
        document = self.document_on(record, current)
        provider = RegistryTaskProvider(self.registry, document, FixedSnapshot(current["snapshot"]))
        candidate = provider.candidates()[record.recipe.task_index]
        instance = provider.instance(candidate)
        envs = FreshEnvs(fleet=self.fleet, shape=self.shapes[document.image.pool.vmm], document=document,
                         instance=instance, run_id=f"gate_rerun_{next(self.run_numbers):06d}", tenant=DEFAULT_TENANT,
                         priority=GATE_RERUN_PRIORITY, endpoint=GATE_RERUN_ENDPOINT,
                         ttl_seconds=GATE_RERUN_TTL_SECONDS)
        runner = TaskGateRunner(self.registry.verifier_registry, self.judge, config=self.gate_config)
        report = runner.run(instance.task, self.gate_inputs(envs, provider, candidate, record.recipe))
        updated = GateRecord(task_spec_digest=record.task_spec_digest, template=record.template,
                             task_id=instance.task_id, image=record.image, image_digest=current["image_digest"],
                             pool=record.pool, snapshot=current["snapshot"], report=report, recipe=record.recipe,
                             ran_at=self.clock(), source="image_bump")
        self.reports.record(updated)
        if not report.passed:
            self.review_service.file_item(FiledItem(
                source="gates", title=f"{record.template} task {instance.task_id} fails its gates on {record.image}",
                note="; ".join(report.review_reasons),
                payload={"task_spec_digest": record.task_spec_digest, "image_digest": current["image_digest"],
                         "snapshot": current["snapshot"], "previous_snapshot": record.snapshot}), RERUN_ACTOR)
        return updated

    def gate_inputs(self, envs: FreshEnvs, provider: RegistryTaskProvider, candidate: CandidateTask,
                    recipe: GateRecipe) -> GateInputs:
        """The recorded solutions replayed on the new snapshot, with the scripted oracle where it stood in."""
        scripted = provider.oracle(candidate)
        alternatives = [envs.replay(parsed(actions), f"alternative_{index}")
                        for index, actions in enumerate(recipe.alternatives)]
        if recipe.oracle is not None:
            oracle = parsed(recipe.oracle)
            alternatives.append(envs.replay(scripted, "scripted_oracle"))
        else:
            oracle = scripted
        oracle_runs = tuple(envs.replay(oracle, f"oracle_run_{index}") for index in range(self.gate_config.rerun_count))
        near_misses = tuple(envs.replay(parsed(actions), f"near_miss_{index}")
                            for index, actions in enumerate(recipe.near_misses))
        agent_attempts = (tuple(self.solvability.attempts(envs.instance, self.gate_config.solvability_tries))
                          if self.solvability is not None else ())
        return GateInputs(initial_state=envs.instance.initial_state, ground_truth=envs.instance.ground_truth,
                          oracle_runs=oracle_runs, alternative_solutions=tuple(alternatives), near_misses=near_misses,
                          agent_attempts=agent_attempts, instruction=envs.instance.instruction)

    def fail(self, record: GateRecord, current: dict[str, Any], error: Exception) -> None:
        """A rerun that cannot run goes to review once per snapshot; the task keeps its last report meanwhile."""
        reason = f"{type(error).__name__}: {error}"
        with self.lock:
            self.failed[(record.task_spec_digest, current["snapshot"])] = reason
        self.review_service.file_item(FiledItem(
            source="gates", title=f"{record.template} gates could not rerun on {record.image}", note=reason,
            payload={"task_spec_digest": record.task_spec_digest, "snapshot": current["snapshot"]}), RERUN_ACTOR)

    def publish_images(self, newest: Mapping[tuple[str, str], dict[str, Any]]) -> None:
        """The images catalog shows each image's newest version and its current snapshot per pool."""
        existing = {entry.name: entry for entry in self.catalog_service.registered_entries("images")}
        by_image: dict[str, dict[str, dict[str, Any]]] = {}
        for (image, pool), current in newest.items():
            by_image.setdefault(image, {})[pool] = current
        for image, pools in by_image.items():
            latest = max(pools.values(), key=updated_at)
            entry = existing.get(image) or CatalogEntry(kind="images", name=image, version=latest["image_digest"])
            details = {**entry.details, "image_digest": latest["image_digest"],
                       "snapshots": {pool: current["snapshot"] for pool, current in sorted(pools.items())}}
            self.catalog_service.publish(entry.model_copy(update={"version": latest["image_digest"],
                                                                  "details": details}))
