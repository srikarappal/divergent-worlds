import pytest
from support.experiment_fakes import build_experiment_service, eval_payload

from rlworld.rollout.groups import EnvShape
from rlworld.sdk.client import SnapshotNotFoundError
from rlworld.services.run_snapshots import PIN_HOLDERS, RunSnapshots
from rlworld.specs import dump_spec_document, parse_spec_document, spec_digest
from rlworld.sdk.client import SnapshotSource
from rlworld.tasks.registry import FleetSnapshots, FleetWorlds, UnknownSnapshotError

SNAPSHOT = "sha256:" + "5e" * 32
WORLD_SNAPSHOT = "sha256:" + "3c" * 32


class SnapshotFleet:
    """fleetd's snapshot routes as launches use them: the current snapshot, pins and prefetches."""

    def __init__(self, current=True):
        self.current = current
        self.pins = []
        self.prefetches = []

    def current_snapshot(self, image_digest, pool):
        if not self.current:
            raise SnapshotNotFoundError(404, "unknown_snapshot", "no current snapshot")
        return {"image_digest": image_digest, "pool": pool, "snapshot": SNAPSHOT}

    def pin_snapshot(self, digest, holder):
        self.pins.append((digest, holder))
        return {"snapshot": digest, "holder": holder}

    def prefetch_snapshot(self, digest, pool, envs, memory_mib, cpu_millicores, gpus=0):
        self.prefetches.append((digest, pool, envs, memory_mib, cpu_millicores, gpus))
        return ["kvm_1", "kvm_2"]

    def prepare_world(self, base_digest, world_digest, payload, apps, pool, memory_mib, cpu_millicores, vcpus):
        return {"base": base_digest, "world": world_digest, "snapshot": WORLD_SNAPSHOT}


def prepared_service(example_payload, tmp_path, fleet):
    payload = eval_payload(example_payload)
    vmm = payload["image"]["pool"]["vmm"]
    shapes = {vmm: EnvShape(pool="windows", memory_mib=6144, vcpus=2, cpu_millicores=2000)}
    service = build_experiment_service(payload, tmp_path)
    service.run_snapshots = RunSnapshots(fleet, FleetSnapshots(fleet, {vmm: "windows"}), shapes)
    return service, service.save_spec(dump_spec_document(parse_spec_document(payload)))


def test_launching_pins_and_prefetches_every_runs_snapshot(example_payload, tmp_path):
    fleet = SnapshotFleet()
    service, digest = prepared_service(example_payload, tmp_path, fleet)
    experiment = service.launch(digest, budget_usd=100.0)
    assert sorted(fleet.pins) == sorted((SNAPSHOT, f"eval_report:{run_id}") for run_id in experiment.run_ids)
    run_id = experiment.run_ids[0]
    document = service.run_document(run_id)
    assert fleet.prefetches[0] == (SNAPSHOT, "windows", document.run.max_concurrent_envs, 6144, 2000, 0)
    events = service.run_events(run_id, 0)
    assert [event.kind for event in events] == ["snapshot", "launched"]
    assert events[0].detail == {"snapshot": SNAPSHOT, "world_snapshot": None, "pinned_for": f"eval_report:{run_id}",
                                "prefetching_on": ["kvm_1", "kvm_2"]}
    assert service.run(run_id).state == "running"


def test_a_run_without_a_snapshot_fails_to_launch_visibly(example_payload, tmp_path):
    fleet = SnapshotFleet(current=False)
    service, digest = prepared_service(example_payload, tmp_path, fleet)
    experiment = service.launch(digest, budget_usd=100.0)
    run_id = experiment.run_ids[0]
    assert service.run(run_id).state == "failed" and fleet.pins == []
    failed = service.run_events(run_id, 0)[-1]
    assert failed.kind == "launch_failed" and failed.detail["error"].startswith("UnknownSnapshotError")
    assert "no snapshot of image" in failed.detail["error"]


def test_each_run_type_pins_for_what_it_produces(example_payload, tmp_path):
    fleet = SnapshotFleet()
    service, digest = prepared_service(example_payload, tmp_path, fleet)
    run_id = service.launch(digest, budget_usd=100.0).run_ids[0]
    run, document = service.run(run_id), service.run_document(run_id)
    for run_type, holder in (("rl", "training_run"), ("eval", "eval_report"), ("data", "dataset")):
        detail = service.run_snapshots.prepare(run.model_copy(update={"run_type": run_type}), document)
        assert detail["pinned_for"] == f"{holder}:{run_id}" and PIN_HOLDERS[run_type] == holder
    assert spec_digest(document.image).startswith("sha256:")
    with pytest.raises(UnknownSnapshotError):
        RunSnapshots(SnapshotFleet(current=False), FleetSnapshots(SnapshotFleet(current=False), {}),
                     {}).prepare(run, document)


def test_a_world_snapshot_is_pinned_and_prefetched_with_its_base(example_payload, tmp_path):
    fleet = SnapshotFleet()
    service, digest = prepared_service(example_payload, tmp_path, fleet)
    service.run_snapshots.worlds = FleetWorlds(fleet, service.run_snapshots.shapes)
    run_id = service.launch(digest, budget_usd=100.0).run_ids[0]
    holder = f"eval_report:{run_id}"
    assert (SNAPSHOT, holder) in fleet.pins and (WORLD_SNAPSHOT, holder) in fleet.pins
    assert {prefetch[0] for prefetch in fleet.prefetches} == {SNAPSHOT, WORLD_SNAPSHOT}
    detail = service.run_events(run_id, 0)[0].detail
    assert (detail["snapshot"], detail["world_snapshot"]) == (SNAPSHOT, WORLD_SNAPSHOT)
    assert isinstance(service.run_snapshots.snapshots.snapshot(service.run_document(run_id).image), SnapshotSource)
