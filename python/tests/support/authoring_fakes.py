"""An authoring bench for service tests: envs are home folders on this machine, so oracles really run."""

import itertools
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import openpyxl
import yaml
from support.experiment_fakes import build_experiment_service, eval_payload

from rlworld.actions.models import ExecAction
from rlworld.images.linux import GUEST_PYTHON
from rlworld.rewards.records import EpisodeOutcome
from rlworld.rollout.groups import EnvShape
from rlworld.sdk.client import (
    FleetError,
    LeaseInfo,
    NoCapacityError,
    SnapshotNotFoundError,
    SnapshotSource,
    StepResult,
    TakenOverError,
)
from rlworld.services.authoring import AuthoringService
from rlworld.services.experiments import ExperimentService
from rlworld.services.review import ReviewService
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.specs import dump_spec_document, parse_spec_document, spec_digest
from rlworld.tasks.finance import oracle_script
from rlworld.tasks.gates import SolutionAttempt
from rlworld.tasks.generators import GeneratedTask
from rlworld.tasks.registry import StaticSnapshots, default_task_registry
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore
from rlworld.worlds import extract_world_state, materialize_world
from rlworld.worlds.integrations import USER_WORLD_FOLDER
from rlworld.worlds.model import World

EXAMPLE_SPEC = Path(__file__).resolve().parents[3] / "examples" / "specs" / "reconcile_invoices_linux.yaml"
WORLD_SIZE = {"employees": 6, "customers": 6, "invoices_per_customer": 3, "emails_per_person": 2}
APPS = ("mail", "files")
SNAPSHOT = SnapshotSource(digest="sha256:dev", state_path="", memory_path="", base_disk_path="rlworld-dev/noble:1")
SHAPES = {"firecracker": EnvShape(pool="linux_dev", memory_mib=4096, vcpus=2)}
DONE = EpisodeOutcome(steps_taken=1, final_action="done")
# A click at (40, 50) then typing 1, as fleetd's recorder reports the author's noVNC input.
EVENTS = [
    {"at_ms": 100, "kind": "pointer", "x": 40, "y": 50, "buttons": 1, "keysym": 0, "down": False},
    {"at_ms": 150, "kind": "pointer", "x": 40, "y": 50, "buttons": 0, "keysym": 0, "down": False},
    {"at_ms": 300, "kind": "key", "x": 0, "y": 0, "buttons": 0, "keysym": 0x31, "down": True},
    {"at_ms": 305, "kind": "key", "x": 0, "y": 0, "buttons": 0, "keysym": 0x31, "down": False},
]
RECORDED_ACTIONS = [{"name": "click", "x": 40, "y": 50, "button": "left", "count": 1, "modifiers": []},
                    {"name": "type", "text": "1"}]


class LocalFleet:
    """Envs are home folders: inject materializes the task world, exec actions run as the user, finish extracts.

    GUI input does nothing here, which is how a recorded oracle that only clicks shows up as not reproducing.
    """

    def __init__(self, root):
        self.root = root
        self.homes = {}
        self.requests = []
        self.calls = []
        self.failing = set()
        self.events = EVENTS
        self.lost_reason = ""
        self.lease_numbers = itertools.count(1)
        self.lock = threading.Lock()
        self.lease_gate = threading.Event()
        self.lease_gate.set()
        self.currents = []
        self.manifests = {}

    def raise_if_failing(self, method_name):
        if method_name == "lease_group" and "no_capacity" in self.failing:
            raise NoCapacityError(503, "no_capacity", "no host can place the request")
        if method_name == "start_takeover" and "taken_over" in self.failing:
            raise TakenOverError(409, "taken_over", "an operator has taken over this env")
        if method_name in self.failing:
            raise FleetError(502, "node_unavailable", f"{method_name} failed on the node")

    def record(self, *call):
        with self.lock:
            self.calls.append(call)

    def calls_to(self, method_name):
        with self.lock:
            return [call for call in self.calls if call[0] == method_name]

    def world_root(self, lease_id):
        return self.homes[lease_id] / USER_WORLD_FOLDER

    def lease_group(self, request):
        self.lease_gate.wait(timeout=30)
        self.raise_if_failing("lease_group")
        with self.lock:
            self.requests.append(request)
            lease_id = f"lease_{next(self.lease_numbers):04d}"
            self.homes[lease_id] = self.root / lease_id
        return [LeaseInfo(lease_id, f"env_{lease_id}", "host_1", "2026-10-03T13:00:00Z")]

    def inject(self, lease_id, payload):
        self.record("inject", lease_id)
        self.raise_if_failing("inject")
        materialize_world(World.model_validate(payload["world"]), tuple(payload["apps"]), self.world_root(lease_id))

    def step(self, lease_id, action):
        self.record("step", lease_id, action.name)
        self.raise_if_failing("step")
        if isinstance(action, ExecAction):
            command = action.command.replace(GUEST_PYTHON, shlex.quote(sys.executable), 1)
            subprocess.run(["bash", "-c", command], env={**os.environ, "HOME": str(self.homes[lease_id])},
                           check=True)
        return StepResult(observation={}, env_seconds=0.1, settle_seconds=0.0, settle_capped=False)

    def finish(self, lease_id, apps):
        self.record("finish", lease_id, tuple(apps))
        self.raise_if_failing("finish")
        return extract_world_state(tuple(apps), self.world_root(lease_id))

    def close(self, lease_id):
        self.record("close", lease_id)
        self.raise_if_failing("close")

    def start_takeover(self, lease_id):
        self.record("start_takeover", lease_id)
        self.raise_if_failing("start_takeover")
        credential = f"{lease_id}_{'c' * 54}"
        return {"session_id": credential, "relay_path": f"/v1/takeover/{credential}",
                "expires_at": "2026-10-03T12:02:00Z"}

    def end_takeover(self, lease_id):
        self.record("end_takeover", lease_id)
        self.raise_if_failing("end_takeover")
        return {"events": self.events, "lost_reason": self.lost_reason, "started_at": "2026-10-03T12:00:00Z",
                "ended_at": "2026-10-03T12:03:00Z"}

    def current_snapshots(self):
        with self.lock:
            return [dict(current) for current in self.currents]

    def snapshot(self, digest):
        with self.lock:
            if digest not in self.manifests:
                raise SnapshotNotFoundError(404, "unknown_snapshot", f"no snapshot {digest}")
            return {"manifest": self.manifests[digest], "pins": []}

    def publish_snapshot(self, image, image_digest, pool, digest, updated_at, image_spec=None):
        """What a fleetd snapshot build leaves behind: the image's current snapshot on a pool."""
        with self.lock:
            self.currents = [current for current in self.currents
                             if (current["image_digest"], current["pool"]) != (image_digest, pool)]
            self.currents.append({"image": image, "image_digest": image_digest, "pool": pool, "snapshot": digest,
                                  "updated_at": updated_at})
            self.manifests[digest] = {"digest": digest, "image": image, "image_digest": image_digest,
                                      "image_spec": image_spec}

    def run_script(self, lease_id, script):
        """What the author does by hand during a recording, done here with the oracle's own script."""
        subprocess.run([sys.executable, "-c", script], env={**os.environ, "HOME": str(self.homes[lease_id])},
                       check=True)


class OracleAgent:
    """A strong agent that plays the scripted oracle, each try in its own fresh home."""

    def __init__(self, root):
        self.root = root
        self.calls = []

    def attempts(self, document, instance, tries):
        self.calls.append((instance.task_id, tries))
        world = World.model_validate(instance.setup_payload["world"])
        home = self.root / f"agent_{len(self.calls)}"
        materialize_world(world, APPS, home / USER_WORLD_FOLDER)
        task = GeneratedTask(instruction=instance.instruction, world=world, ground_truth=instance.ground_truth)
        subprocess.run([sys.executable, "-c", oracle_script(task)], env={**os.environ, "HOME": str(home)},
                       check=True)
        return [SolutionAttempt("strong_agent_0", extract_world_state(APPS, home / USER_WORLD_FOLDER), DONE)]


class CrashingAgent:
    def attempts(self, document, instance, tries):
        raise RuntimeError("the strong agent's endpoint is down")


@dataclass
class Bench:
    service: AuthoringService
    fleet: LocalFleet
    review: ReviewService
    experiments: ExperimentService
    digest: str


def reconcile_spec_yaml(**difficulty_changes):
    """The Linux reference spec with a smaller world, so every fresh env materializes quickly."""
    payload = yaml.safe_load(EXAMPLE_SPEC.read_text())
    payload["world"]["size"] = WORLD_SIZE
    payload["task"]["difficulty"].update(difficulty_changes)
    return dump_spec_document(parse_spec_document(payload))


def authoring_bench(example_payload, tmp_path, solvability=None, gate_config=None, shapes=None, snapshots=None,
                    gate_reports=None):
    experiments = build_experiment_service(eval_payload(example_payload), tmp_path)
    digest = experiments.save_spec(reconcile_spec_yaml())
    image = experiments.spec(digest)[1].image
    fleet = LocalFleet(tmp_path / "envs")
    review = ReviewService(TrajectoryViews(TrajectoryStore(InMemoryObjectStore())), clock=float)
    service = AuthoringService(experiments, default_task_registry(),
                               snapshots or StaticSnapshots({spec_digest(image): SNAPSHOT}), fleet, review,
                               SHAPES if shapes is None else shapes, solvability=solvability,
                               gate_config=gate_config, gate_reports=gate_reports, clock=float)
    return Bench(service, fleet, review, experiments, digest)


def generated_task(bench, session_id):
    draft = bench.service.draft(session_id)
    return draft.provider.generated_task(draft.candidate)


def edit_workbook(world_root, cells, cleared=()):
    path = world_root / "files" / "reconciliation.xlsx"
    workbook = openpyxl.load_workbook(path)
    for cell_name, value in cells.items():
        workbook.active[cell_name] = value
    for cell_name in cleared:
        workbook.active[cell_name] = None
    workbook.save(path)


def record_alternative(bench, session_id):
    """The author solves the task with the totals typed as text with thousands separators, which must still pass."""
    started = bench.service.begin_recording(session_id, "alternative", "ada")
    task = generated_task(bench, session_id)
    bench.fleet.run_script(started.recording.lease_id, oracle_script(task))
    totals = task.ground_truth["totals"]
    edit_workbook(bench.fleet.world_root(started.recording.lease_id),
                  {"A1": "Open ", "B1": f"{totals['open']:,.2f}", "B2": f"{totals['overdue']:,.2f}"})
    return bench.service.end_recording(session_id, started.recording.recording_id)


def record_near_miss(bench, session_id):
    """The right open total in the wrong cell: the verifiers must reject it."""
    started = bench.service.begin_recording(session_id, "near_miss", "ada")
    task = generated_task(bench, session_id)
    bench.fleet.run_script(started.recording.lease_id, oracle_script(task))
    edit_workbook(bench.fleet.world_root(started.recording.lease_id),
                  {"C1": task.ground_truth["totals"]["open"]}, cleared=("B1",))
    return bench.service.end_recording(session_id, started.recording.recording_id)
