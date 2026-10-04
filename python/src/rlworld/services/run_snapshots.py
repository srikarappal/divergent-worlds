"""What scheduling a run does to its snapshot (DESIGN.md section 6.2).

Before a run's workers or trainer start, its snapshot is pinned for the run so
retention never removes what a training run, an eval report or a dataset was
made from. It is also prefetched onto enough hosts for the run's concurrency, so
its first leases find the snapshot on local NVMe instead of pulling it. When the
run's world gets a world snapshot, that is pinned and prefetched as well.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from rlworld.rollout.groups import EnvShape
from rlworld.services.experiments import RunRecord
from rlworld.specs.document import SpecDocument
from rlworld.tasks.registry import SnapshotResolver, WorldPreparer
from rlworld.worlds.generator import build_world

# What each run type's pin stands for: training runs, the eval reports eval runs produce, the datasets of data runs.
PIN_HOLDERS = {"rl": "training_run", "eval": "eval_report", "data": "dataset"}


class SnapshotFleet(Protocol):
    def pin_snapshot(self, digest: str, holder: str) -> dict[str, Any]: ...

    def prefetch_snapshot(self, digest: str, pool: str, envs: int, memory_mib: int, cpu_millicores: int,
                          gpus: int = 0) -> list[str]: ...


class RunSnapshots:
    def __init__(self, fleet: SnapshotFleet, snapshots: SnapshotResolver, shapes: Mapping[str, EnvShape],
                 worlds: WorldPreparer | None = None) -> None:
        self.fleet = fleet
        self.snapshots = snapshots
        self.shapes = dict(shapes)
        self.worlds = worlds

    def prepare(self, run: RunRecord, document: SpecDocument) -> dict[str, Any]:
        """Pins the run's snapshots and starts their prefetch; what it did becomes the run's snapshot event. The
        first run of a world on a base snapshot waits while fleetd decides whether the world needs its own."""
        source = self.snapshots.snapshot(document.image)
        world_snapshot = None
        if self.worlds is not None:
            world_snapshot = self.worlds.prepare(source, build_world(document.world),
                                                 tuple(document.world.materialize), document.image)
        holder = f"{PIN_HOLDERS[run.run_type]}:{run.run_id}"
        shape = self.shapes[document.image.pool.vmm]
        hosts: list[str] = []
        for digest in (source.digest, *([world_snapshot.digest] if world_snapshot else [])):
            self.fleet.pin_snapshot(digest, holder)
            hosts = self.fleet.prefetch_snapshot(digest, shape.pool, document.run.max_concurrent_envs,
                                                 shape.memory_mib, shape.cpu_millicores)
        return {"snapshot": source.digest, "world_snapshot": world_snapshot.digest if world_snapshot else None,
                "pinned_for": holder, "prefetching_on": hosts}
