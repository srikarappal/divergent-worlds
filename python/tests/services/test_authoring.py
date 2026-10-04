import itertools
import json
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx
import openpyxl
import pytest
import yaml
from support.experiment_fakes import FlaskBridge, build_experiment_service, eval_payload

from rlworld.actions.models import AnswerAction, AskUserAction, ClickAction, DoneAction, ExecAction, TypeAction
from rlworld.cli import main
from rlworld.images.linux import GUEST_PYTHON
from rlworld.rewards.records import EpisodeOutcome
from rlworld.rollout.groups import EnvShape
from rlworld.sdk import ExperimentClient
from rlworld.sdk.client import FleetError, LeaseInfo, NoCapacityError, SnapshotSource, StepResult, TakenOverError
from rlworld.services.admin import PlatformAuditLog
from rlworld.services.auth import BUILDERS, EVERYONE, allowed_roles
from rlworld.services.authoring import (
    AUTHORING_LEASE_TTL_SECONDS,
    AuthoringError,
    AuthoringService,
    playable,
)
from rlworld.services.experimentd_http import create_app
from rlworld.services.experiments import ExperimentService, UnknownRecordError
from rlworld.services.review import ReviewService
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.specs import dump_spec_document, parse_spec_document, spec_digest
from rlworld.tasks.finance import oracle_script
from rlworld.tasks.gates import GateConfig, SolutionAttempt
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
GATE_NAMES = ["null_action", "oracle", "rerun_stability", "alternative_solution", "near_miss", "solvability"]
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

    def run_script(self, lease_id, script):
        """What the author does by hand during a recording, done here with the oracle's own script."""
        subprocess.run([sys.executable, "-c", script], env={**os.environ, "HOME": str(self.homes[lease_id])},
                       check=True)


class OracleAgent:
    """A strong agent that plays the scripted oracle, each try in its own fresh home."""

    def __init__(self, root):
        self.root = root
        self.calls = []

    def attempts(self, instance, tries):
        self.calls.append((instance.task_id, tries))
        world = World.model_validate(instance.setup_payload["world"])
        home = self.root / f"agent_{len(self.calls)}"
        materialize_world(world, APPS, home / USER_WORLD_FOLDER)
        task = GeneratedTask(instruction=instance.instruction, world=world, ground_truth=instance.ground_truth)
        subprocess.run([sys.executable, "-c", oracle_script(task)], env={**os.environ, "HOME": str(home)},
                       check=True)
        return [SolutionAttempt("strong_agent_0", extract_world_state(APPS, home / USER_WORLD_FOLDER), DONE)]


class CrashingAgent:
    def attempts(self, instance, tries):
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


def authoring_bench(example_payload, tmp_path, solvability=None, gate_config=None, shapes=None, snapshots=None):
    experiments = build_experiment_service(eval_payload(example_payload), tmp_path)
    digest = experiments.save_spec(reconcile_spec_yaml())
    image = experiments.spec(digest)[1].image
    fleet = LocalFleet(tmp_path / "envs")
    review = ReviewService(TrajectoryViews(TrajectoryStore(InMemoryObjectStore())), clock=float)
    service = AuthoringService(experiments, default_task_registry(),
                               snapshots or StaticSnapshots({spec_digest(image): SNAPSHOT}), fleet, review,
                               SHAPES if shapes is None else shapes, solvability=solvability,
                               gate_config=gate_config, clock=float)
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


def test_a_session_starts_from_a_saved_spec_with_one_generated_task(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path)
    session = bench.service.start(bench.digest, "ada", "finance_lab", task_index=1)
    draft = bench.service.draft(session.session_id)
    candidates = draft.provider.candidates()
    assert len(candidates) == 4 and session.task_id == candidates[1].task_id == draft.instance.task_id
    assert (session.session_id, session.spec_digest, session.author, session.tenant, session.template) == (
        "authoring_000001", bench.digest, "ada", "finance_lab", "tasks.finance.reconcile_invoices")
    assert session.instruction == draft.instance.instruction and "invoices.csv" in session.instruction
    assert session.hidden_intent == "" and session.created_at == 0.0
    assert (session.gate_status, session.recordings, session.gate_results, session.review_item_id) == (
        "not_run", [], [], None)
    copy = bench.service.session(session.session_id)
    copy.gate_status = "passed"
    assert bench.service.session(session.session_id).gate_status == "not_run"
    assert bench.service.start(bench.digest, "ada", "finance_lab").session_id == "authoring_000002"
    assert bench.fleet.requests == []


def test_sessions_need_a_servable_spec_a_shape_and_a_snapshot(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path)
    with pytest.raises(AuthoringError, match="task_index must be below 4"):
        bench.service.start(bench.digest, "ada", "finance_lab", task_index=4)
    with pytest.raises(UnknownRecordError):
        bench.service.start("sha256:missing", "ada", "finance_lab")
    with pytest.raises(UnknownRecordError):
        bench.service.session("authoring_999999")
    shapeless = AuthoringService(bench.experiments, default_task_registry(), bench.service.snapshots, bench.fleet,
                                 bench.review, {"qemu_templated": SHAPES["firecracker"]})
    with pytest.raises(AuthoringError, match="no env shape is configured for firecracker pools"):
        shapeless.start(bench.digest, "ada", "finance_lab")
    unsnapshotted = AuthoringService(bench.experiments, default_task_registry(), StaticSnapshots({}), bench.fleet,
                                     bench.review, SHAPES)
    with pytest.raises(AuthoringError, match="no snapshot of image noble_calc_v1"):
        unsnapshotted.start(bench.digest, "ada", "finance_lab")
    three_apps = bench.experiments.save_spec(reconcile_spec_yaml(apps=3))
    with pytest.raises(AuthoringError, match="at most 2 apps"):
        bench.service.start(three_apps, "ada", "finance_lab")


def test_a_recording_hands_over_a_dev_env_at_the_task_start_state(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path)
    session = bench.service.start(bench.digest, "ada", "finance_lab")
    started = bench.service.begin_recording(session.session_id, "oracle", "ada")
    request = bench.fleet.requests[0]
    assert (request.run_id, request.tenant, request.pool, request.priority, request.endpoint) == (
        session.session_id, "finance_lab", "linux_dev", "interactive", "authoring")
    assert (request.snapshot, request.memory_mib, request.vcpus, request.ttl_seconds) == (
        SNAPSHOT, 4096, 2, AUTHORING_LEASE_TTL_SECONDS)
    assert request.world_time == "2026-03-14T09:00:00"
    assert request.observation_kinds == ("screenshot", "accessibility_tree")
    lease_id = started.recording.lease_id
    assert (bench.fleet.world_root(lease_id) / "files" / "invoices.csv").exists()
    assert started.relay_path == f"/v1/takeover/{started.takeover_session_id}"
    assert (started.recording.kind, started.recording.status, started.recording.recorded_by) == (
        "oracle", "recording", "ada")
    stored = bench.service.session(session.session_id)
    assert [recording.status for recording in stored.recordings] == ["recording"]
    assert started.takeover_session_id not in stored.model_dump_json()
    bench.fleet.run_script(lease_id, oracle_script(generated_task(bench, session.session_id)))
    recording = bench.service.end_recording(session.session_id, started.recording.recording_id)
    assert (recording.status, recording.actions, recording.complete, recording.error) == (
        "recorded", RECORDED_ACTIONS, True, "")
    assert [call[0] for call in bench.fleet.calls if call[1] == lease_id] == [
        "inject", "start_takeover", "end_takeover", "finish", "close"]
    assert bench.fleet.calls_to("finish") == [("finish", lease_id, APPS)]
    attempt = bench.service.draft(session.session_id).attempts[recording.recording_id]
    assert attempt.name == f"oracle_{recording.recording_id}" and attempt.outcome.steps_taken == 2
    assert attempt.final_state["files"]["reconciliation.xlsx"]["kind"] == "spreadsheet"
    with pytest.raises(AuthoringError, match="is recorded, not recording"):
        bench.service.end_recording(session.session_id, recording.recording_id)
    with pytest.raises(UnknownRecordError):
        bench.service.end_recording(session.session_id, "recording_999999")


def test_a_recording_that_lost_input_is_kept_but_marked_incomplete(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path)
    session = bench.service.start(bench.digest, "ada", "finance_lab")
    started = bench.service.begin_recording(session.session_id, "near_miss", "ada")
    bench.fleet.lost_reason = "the relay dropped 12 input events"
    recording = bench.service.end_recording(session.session_id, started.recording.recording_id)
    assert (recording.status, recording.complete, recording.lost_reason) == (
        "recorded", False, "the relay dropped 12 input events")


def test_recordings_refuse_cleanly_when_the_fleet_cannot_serve_them(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path)
    session_id = bench.service.start(bench.digest, "ada", "finance_lab").session_id
    bench.fleet.failing = {"no_capacity"}
    with pytest.raises(AuthoringError, match="no dev env: 503 no_capacity"):
        bench.service.begin_recording(session_id, "oracle", "ada")
    bench.fleet.failing = {"inject"}
    with pytest.raises(AuthoringError, match="no dev env: 502 node_unavailable: inject failed"):
        bench.service.begin_recording(session_id, "oracle", "ada")
    bench.fleet.failing = {"taken_over"}
    with pytest.raises(AuthoringError, match="cannot be handed over: 409 taken_over"):
        bench.service.begin_recording(session_id, "oracle", "ada")
    assert [call[1] for call in bench.fleet.calls_to("close")] == ["lease_0001", "lease_0002"]
    assert bench.service.session(session_id).recordings == []
    bench.fleet.failing = set()
    started = bench.service.begin_recording(session_id, "oracle", "ada")
    bench.fleet.failing = {"finish", "close"}
    recording = bench.service.end_recording(session_id, started.recording.recording_id)
    assert (recording.status, recording.error) == ("failed", "502 node_unavailable: finish failed on the node")
    assert recording.recording_id not in bench.service.draft(session_id).attempts
    assert bench.fleet.calls_to("close")[-1] == ("close", started.recording.lease_id)
    bench.fleet.failing = set()
    bench.fleet.events = [{"at_ms": 1, "kind": "teleport"}]
    malformed = bench.service.begin_recording(session_id, "oracle", "ada")
    failed = bench.service.end_recording(session_id, malformed.recording.recording_id)
    assert failed.status == "failed" and "teleport" in failed.error
    assert bench.fleet.calls_to("close")[-1] == ("close", malformed.recording.lease_id)


def test_the_scripted_oracle_recordings_and_a_strong_agent_pass_every_gate(example_payload, tmp_path):
    agent = OracleAgent(tmp_path / "agent")
    bench = authoring_bench(example_payload, tmp_path, solvability=agent)
    session_id = bench.service.start(bench.digest, "ada", "finance_lab").session_id
    assert record_alternative(bench, session_id).status == "recorded"
    assert record_near_miss(bench, session_id).status == "recorded"
    replays_before = len(bench.fleet.requests)
    session = bench.service.run_gates(session_id)
    assert [result.name for result in session.gate_results] == GATE_NAMES
    assert session.gate_status == "passed", [(result.name, result.detail) for result in session.gate_results]
    assert len(bench.fleet.requests) - replays_before == 3
    replayed = [lease_id for lease_id in bench.fleet.homes if lease_id not in ("lease_0001", "lease_0002")]
    assert [call[1:] for call in bench.fleet.calls_to("step")] == [(lease_id, "exec") for lease_id in replayed]
    assert agent.calls == [(session.task_id, 8)]
    alternative = next(result for result in session.gate_results if result.name == "alternative_solution")
    assert [attempt["name"] for attempt in alternative.attempts] == ["alternative_recording_000001"]
    near_miss = next(result for result in session.gate_results if result.name == "near_miss")
    assert near_miss.attempts[0]["failed_checks"] and near_miss.attempts[0]["termination"] != "success"
    submitted = bench.service.submit(session_id, "ada")
    item = bench.review.list_items(queue="review")[0]
    assert submitted.review_item_id == item.item_id
    assert (item.action, item.source, item.created_by, item.note) == (
        "task_submission", "authoring", "ada", "authored by ada")
    assert item.title == f"tasks.finance.reconcile_invoices task {session.task_id} passed its gates"
    assert item.payload["gate_status"] == "passed" and len(item.payload["gate_results"]) == 6
    assert item.payload["recordings"] == [
        {"recording_id": "recording_000001", "kind": "alternative", "status": "recorded", "actions": 2},
        {"recording_id": "recording_000002", "kind": "near_miss", "status": "recorded", "actions": 2}]
    assert "ground_truth" not in json.dumps(item.payload)
    with pytest.raises(AuthoringError, match="already submitted"):
        bench.service.submit(session_id, "ada")
    with pytest.raises(AuthoringError, match="already submitted"):
        bench.service.begin_recording(session_id, "oracle", "ada")
    with pytest.raises(AuthoringError, match="already submitted"):
        bench.service.start_gates(session_id)


def test_a_recorded_oracle_replaces_the_scripted_one_and_must_reproduce(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path)
    session_id = bench.service.start(bench.digest, "ada", "finance_lab").session_id
    started = bench.service.begin_recording(session_id, "oracle", "ada")
    bench.fleet.run_script(started.recording.lease_id, oracle_script(generated_task(bench, session_id)))
    bench.service.end_recording(session_id, started.recording.recording_id)
    session = bench.service.run_gates(session_id)
    results = {result.name: result for result in session.gate_results}
    assert session.gate_status == "failed"
    assert not results["oracle"].passed and results["oracle"].detail.startswith("the oracle does not pass")
    assert results["rerun_stability"].passed and len(results["rerun_stability"].attempts) == 3
    assert results["alternative_solution"].passed
    assert [attempt["name"] for attempt in results["alternative_solution"].attempts] == ["scripted_oracle"]
    assert results["near_miss"].detail == "no near miss was provided, so false positives go unseen"
    assert results["solvability"].detail == "no strong agent attempts were provided"
    steps = [call[2] for call in bench.fleet.calls_to("step")]
    assert steps.count("exec") == 1 and steps.count("click") == 3 and steps.count("type") == 3
    submitted = bench.service.submit(session_id, "ada")
    item = bench.review.list_items()[0]
    assert submitted.review_item_id == item.item_id and item.title.endswith("failed its gates")


def test_env_failures_during_replays_become_gate_results(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path, gate_config=GateConfig(rerun_count=2, solvability_tries=1))
    session_id = bench.service.start(bench.digest, "ada", "finance_lab").session_id
    bench.fleet.failing = {"finish"}
    session = bench.service.run_gates(session_id)
    results = {result.name: result for result in session.gate_results}
    assert session.gate_status == "failed" and session.gate_error == ""
    assert "replay failed: 502 node_unavailable: finish failed on the node" in results["oracle"].detail
    assert len(bench.fleet.calls_to("close")) == 2
    bench.fleet.failing = {"no_capacity"}
    session = bench.service.run_gates(session_id)
    assert "lease failed: 503 no_capacity" in next(result.detail for result in session.gate_results
                                                   if result.name == "oracle")


def test_gates_run_on_their_own_thread_and_never_overlap(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path, solvability=CrashingAgent())
    session_id = bench.service.start(bench.digest, "ada", "finance_lab").session_id
    with pytest.raises(AuthoringError, match="run the gates before submitting"):
        bench.service.submit(session_id, "ada")
    bench.fleet.lease_gate.clear()
    started = bench.service.start_gates(session_id)
    assert started.gate_status == "running" and bench.service.session(session_id).gate_status == "running"
    with pytest.raises(AuthoringError, match="gates already run"):
        bench.service.start_gates(session_id)
    with pytest.raises(AuthoringError, match="run the gates before submitting"):
        bench.service.submit(session_id, "ada")
    bench.fleet.lease_gate.set()
    bench.service.gate_runs[session_id].join(timeout=60)
    session = bench.service.session(session_id)
    assert session.gate_status == "error" and session.gate_results == []
    assert session.gate_error == "RuntimeError: the strong agent's endpoint is down"
    with pytest.raises(AuthoringError, match="run the gates before submitting"):
        bench.service.submit(session_id, "ada")
    bench.service.solvability = None
    rerun = bench.service.start_gates(session_id)
    bench.service.gate_runs[session_id].join(timeout=60)
    assert rerun.gate_status == "running" and bench.service.session(session_id).gate_status == "failed"


def test_playable_stops_at_the_first_ending_action_and_skips_control_actions():
    exec_action = ExecAction(command="true")
    steps, outcome = playable([AskUserAction(question="which month?"), exec_action,
                               AnswerAction(text="42.00"), ClickAction(x=1, y=1)])
    assert steps == [exec_action]
    assert (outcome.steps_taken, outcome.final_action, outcome.answer_text) == (1, "answer", "42.00")
    steps, outcome = playable([ClickAction(x=1, y=1), TypeAction(text="1")])
    assert len(steps) == 2 and (outcome.steps_taken, outcome.final_action) == (2, "done")
    assert playable([DoneAction()]) == ([], EpisodeOutcome(steps_taken=0, final_action="done"))


def test_authoring_writes_are_for_builders_and_reads_for_everyone():
    for rule in ("/v1/authoring", "/v1/authoring/<session_id>/recordings",
                 "/v1/authoring/<session_id>/recordings/<recording_id>/end", "/v1/authoring/<session_id>/gates",
                 "/v1/authoring/<session_id>/submit"):
        assert allowed_roles("POST", rule) == BUILDERS
    assert allowed_roles("GET", "/v1/authoring/<session_id>") == EVERYONE


def bridged(app):
    return httpx.Client(transport=httpx.MockTransport(FlaskBridge(app)), base_url="http://experimentd.internal")


def test_authoring_routes_follow_the_flow_and_never_audit_the_relay_credential(example_payload, tmp_path):
    bench = authoring_bench(example_payload, tmp_path)
    audit = PlatformAuditLog()
    client = bridged(create_app(bench.experiments, audit_log_store=audit, authoring_service=bench.service,
                                review_service=bench.review))
    assert client.post("/v1/authoring", json={}).json()["code"] == "invalid_request"
    created = client.post("/v1/authoring", json={"spec_digest": bench.digest, "task_index": 2})
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    assert client.get(f"/v1/authoring/{session_id}").json()["task_id"] == created.json()["task_id"]
    assert client.post(f"/v1/authoring/{session_id}/recordings", json={"kind": "bogus"}).status_code == 400
    started = client.post(f"/v1/authoring/{session_id}/recordings", json={"kind": "near_miss"})
    assert started.status_code == 201 and started.json()["recording"]["kind"] == "near_miss"
    credential = started.json()["takeover_session_id"]
    recording_id = started.json()["recording"]["recording_id"]
    ended = client.post(f"/v1/authoring/{session_id}/recordings/{recording_id}/end")
    assert ended.status_code == 200 and ended.json()["actions"] == RECORDED_ACTIONS
    assert client.post(f"/v1/authoring/{session_id}/recordings/{recording_id}/end").json()["code"] == (
        "authoring_refused")
    refused = client.post(f"/v1/authoring/{session_id}/submit")
    assert refused.status_code == 409 and "run the gates" in refused.json()["problems"][0]
    gates = client.post(f"/v1/authoring/{session_id}/gates")
    assert gates.status_code == 202 and gates.json()["gate_status"] == "running"
    bench.service.gate_runs[session_id].join(timeout=60)
    assert client.get(f"/v1/authoring/{session_id}").json()["gate_status"] == "failed"
    submitted = client.post(f"/v1/authoring/{session_id}/submit")
    assert submitted.status_code == 200
    items = client.get("/v1/review_items", params={"queue": "review"}).json()["items"]
    assert [item["item_id"] for item in items] == [submitted.json()["review_item_id"]]
    assert client.get("/v1/authoring/authoring_999999").status_code == 404
    records = {record.action: record for record in audit.query()}
    assert set(records) == {"start_authoring", "begin_recording", "end_recording", "run_authoring_gates",
                            "submit_authoring"}
    assert records["start_authoring"].detail == {"spec_digest": bench.digest, "task_index": 2,
                                                 "session_id": session_id, "task_id": created.json()["task_id"]}
    assert records["begin_recording"].detail == {"kind": "near_miss"}
    assert records["begin_recording"].target == {"session_id": session_id}
    assert records["submit_authoring"].detail == {"review_item_id": submitted.json()["review_item_id"]}
    assert credential not in json.dumps([record.model_dump(mode="json") for record in audit.query()])
    unconfigured = bridged(create_app(bench.experiments))
    assert unconfigured.post("/v1/authoring", json={"spec_digest": bench.digest}).status_code == 503


def test_the_cli_and_the_sdk_drive_the_same_authoring_flow(example_payload, tmp_path, capsys):
    bench = authoring_bench(example_payload, tmp_path)
    app = create_app(bench.experiments, authoring_service=bench.service, review_service=bench.review)
    client = ExperimentClient("http://experimentd.internal", httpx.Client(transport=httpx.MockTransport(
        FlaskBridge(app))))
    assert main(["authoring", "start", bench.digest, "--task-index", "3"], client=client) == 0
    session = json.loads(capsys.readouterr().out)
    session_id = session["session_id"]
    assert main(["authoring", "record", session_id, "--kind", "alternative"], client=client) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["relay_path"] == f"/v1/takeover/{started['takeover_session_id']}"
    assert main(["authoring", "stop", session_id, started["recording"]["recording_id"]], client=client) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "recorded"
    assert main(["authoring", "gates", session_id], client=client) == 0
    assert json.loads(capsys.readouterr().out)["gate_status"] == "running"
    bench.service.gate_runs[session_id].join(timeout=60)
    assert client.get_authoring(session_id).gate_status == "failed"
    assert main(["authoring", "submit", session_id], client=client) == 0
    assert json.loads(capsys.readouterr().out)["review_item_id"] == "review_000001"
    assert main(["authoring", "submit", session_id], client=client) == 1
    assert "authoring_refused" in capsys.readouterr().err
    assert main(["authoring", "get", session_id], client=client) == 0
    assert json.loads(capsys.readouterr().out)["recordings"][0]["kind"] == "alternative"
    sdk_session = client.start_authoring(bench.digest)
    started_again = client.begin_recording(sdk_session.session_id, kind="oracle")
    assert client.end_recording(sdk_session.session_id, started_again.recording.recording_id).status == "recorded"
    assert client.run_authoring_gates(sdk_session.session_id).gate_status == "running"
    bench.service.gate_runs[sdk_session.session_id].join(timeout=60)
    assert client.submit_authoring(sdk_session.session_id).review_item_id == "review_000002"
