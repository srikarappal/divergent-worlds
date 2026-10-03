import json

import pytest
import yaml

from rlworld.preflight import PreflightContext, SeatPool
from rlworld.services.experimentd_http import create_app
from rlworld.services.experiments import (
    ControlError,
    ExperimentService,
    InvalidRunTokenError,
    LaunchBlockedError,
)
from rlworld.services.trainer_launch import TrainerLauncher, TrainerTemplate
from rlworld.specs import SpecValidationError, expand_sweep, load_spec_document, set_path
from rlworld.specs.digest import spec_digest
from rlworld.tasks import GateReport

TEMPLATE = TrainerTemplate(name="verl_grpo_7b_vlm", image="registry.example.com/verl:0.4",
                           command=("python", "-m", "verl.trainer.main_ppo"))
SEAT_POOLS = {"windows_server_dc": SeatPool(1000, "per_host"), "office_ltsc": SeatPool(10_000)}


class FakeClock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now


class RecordingExecutor:
    def __init__(self, failing_run_ids=()):
        self.started = []
        self.applied = []
        self.failing_run_ids = set(failing_run_ids)

    def start(self, run, document):
        if run.run_id in self.failing_run_ids:
            raise RuntimeError("no rollout workers")
        self.started.append(run.run_id)

    def apply(self, run):
        self.applied.append((run.run_id, run.state, run.desired_concurrency, run.priority))

    def dry_run(self, document):
        return "episode_dry_000001"


class RecordingRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, working_directory):
        self.calls.append(argv)
        return ""


def preflight_context(spec_text, seat_pools=SEAT_POOLS):
    document = load_spec_document(spec_text)
    gate_reports = {spec_digest(point.document.task): GateReport(spec_digest(point.document.task), ())
                    for point in expand_sweep(document)}
    return PreflightContext(gate_reports=gate_reports, seat_pools=seat_pools)


def build_service(spec_text, tmp_path, executor=None, approval_threshold_usd=None, seat_pools=SEAT_POOLS):
    clock = FakeClock()
    runner = RecordingRunner()
    service = ExperimentService(
        preflight_context(spec_text, seat_pools),
        executor or RecordingExecutor(),
        TrainerLauncher({TEMPLATE.name: TEMPLATE}, runner, tmp_path),
        token_secret=b"run token secret",
        fleet_url="http://fleetd.internal",
        experimentd_url="http://experimentd.internal",
        approval_threshold_usd=approval_threshold_usd,
        clock=clock.time,
    )
    return service, runner, clock


def eval_spec_text(example_payload):
    payload = set_path(example_payload, "run.type", "eval")
    payload = set_path(payload, "run.trainer", None)
    payload = set_path(payload, "run.priority", "eval")
    return yaml.safe_dump(payload)


def test_specs_are_immutable_and_validated(example_spec_text, tmp_path):
    service, _, _ = build_service(example_spec_text, tmp_path)
    digest = service.save_spec(example_spec_text)
    assert service.save_spec(example_spec_text) == digest
    assert digest.startswith("sha256:")
    with pytest.raises(SpecValidationError):
        service.save_spec("experiment: {}")
    assert service.preflight(digest).is_valid


def test_launching_rl_submits_one_trainer_job_per_sweep_point(example_spec_text, tmp_path):
    service, runner, _ = build_service(example_spec_text, tmp_path)
    experiment = service.launch(service.save_spec(example_spec_text))
    assert len(experiment.run_ids) == 6 and experiment.approved
    runs = service.list_runs(experiment.experiment_id)
    assert {run.state for run in runs} == {"running"}
    assert runs[0].job_id == "rlworld-experiment-000001-run-000"
    assert runs[0].assignments == {"task.difficulty.corrupted_inputs": 0, "task.difficulty.distractors": 0}
    assert [call[:3] for call in runner.calls].count(["kubectl", "apply", "-f"]) == 6
    manifest = (tmp_path / "rlworld-experiment-000001-run-000.yaml").read_text()
    secret = next(yaml.safe_load_all(manifest))
    assert service.verify_run_token(secret["stringData"]["run_token"]) == "experiment_000001_run_000"
    assert service.run_events(runs[0].run_id)[0].kind == "launched"


def test_launching_eval_starts_platform_hosted_runs(example_payload, tmp_path):
    spec_text = eval_spec_text(example_payload)
    executor = RecordingExecutor(failing_run_ids={"experiment_000001_run_002"})
    service, runner, _ = build_service(spec_text, tmp_path, executor=executor)
    experiment = service.launch(service.save_spec(spec_text))
    assert len(executor.started) == 5
    assert runner.calls == []
    failed_run = service.run("experiment_000001_run_002")
    assert failed_run.state == "failed"
    assert service.run_events(failed_run.run_id)[0].kind == "launch_failed"
    assert service.run(experiment.run_ids[0]).state == "running"
    assert service.dry_run(service.save_spec(spec_text)) == "episode_dry_000001"


def test_preflight_errors_block_launch(example_spec_text, tmp_path):
    service, _, _ = build_service(example_spec_text, tmp_path, seat_pools={})
    with pytest.raises(LaunchBlockedError) as raised:
        service.launch(service.save_spec(example_spec_text))
    assert any("unknown_license_pool" in problem for problem in raised.value.problems)


def test_expensive_or_unpriced_launches_wait_for_approval(example_spec_text, tmp_path):
    service, runner, _ = build_service(example_spec_text, tmp_path, approval_threshold_usd=100.0)
    experiment = service.launch(service.save_spec(example_spec_text))
    assert not experiment.approved
    assert {run.state for run in service.list_runs(experiment.experiment_id)} == {"awaiting_approval"}
    assert runner.calls == []
    assert service.approve(experiment.experiment_id).approved
    assert {run.state for run in service.list_runs(experiment.experiment_id)} == {"running"}


def test_controls_are_operational_only(example_payload, tmp_path):
    spec_text = eval_spec_text(example_payload)
    executor = RecordingExecutor()
    service, _, _ = build_service(spec_text, tmp_path, executor=executor)
    run_id = service.launch(service.save_spec(spec_text)).run_ids[0]
    service.control(run_id, "pause")
    service.control(run_id, "set_concurrency", 64)
    service.control(run_id, "set_priority", "interactive")
    service.control(run_id, "resume")
    assert executor.applied[-1] == (run_id, "running", 64, "interactive")
    budget_before = service.experiment("experiment_000001").budget_usd
    service.control(run_id, "extend_budget", 500)
    assert service.experiment("experiment_000001").budget_usd == budget_before + 500
    for command, value in [("set_concurrency", 0), ("set_priority", "urgent"), ("extend_budget", -1),
                           ("set_judge_model", "other")]:
        with pytest.raises(ControlError):
            service.control(run_id, command, value)
    service.control(run_id, "stop")
    with pytest.raises(ControlError):
        service.control(run_id, "resume")


def test_stopping_an_rl_run_cancels_its_trainer_job(example_spec_text, tmp_path):
    service, runner, _ = build_service(example_spec_text, tmp_path)
    run_id = service.launch(service.save_spec(example_spec_text)).run_ids[0]
    service.control(run_id, "pause")
    assert service.run(run_id).state == "paused"
    service.control(run_id, "stop")
    assert runner.calls[-1] == ["kubectl", "delete", "job", "rlworld-experiment-000001-run-000", "--wait=false"]


def test_crossing_the_budget_stops_every_live_run(example_payload, tmp_path):
    spec_text = eval_spec_text(example_payload)
    service, _, _ = build_service(spec_text, tmp_path)
    experiment = service.launch(service.save_spec(spec_text))
    service.record_spend(experiment.run_ids[0], 1000.0)
    assert service.run(experiment.run_ids[1]).state == "running"
    service.record_spend(experiment.run_ids[1], 1600.0)
    assert {service.run(run_id).state for run_id in experiment.run_ids} == {"stopped"}
    assert service.run_events(experiment.run_ids[3])[-1].kind == "budget_exhausted"


def test_run_tokens_expire_and_die_with_the_run(example_spec_text, tmp_path):
    service, _, clock = build_service(example_spec_text, tmp_path)
    run_id = service.launch(service.save_spec(example_spec_text)).run_ids[0]
    token = service.issue_run_token(run_id)
    assert service.verify_run_token(token) == run_id
    with pytest.raises(InvalidRunTokenError):
        service.verify_run_token(token[:-1] + ("0" if token[-1] != "0" else "1"))
    clock.now += 8 * 86_400
    with pytest.raises(InvalidRunTokenError):
        service.verify_run_token(token)
    clock.now -= 8 * 86_400
    service.mark_finished(run_id, "completed")
    with pytest.raises(InvalidRunTokenError):
        service.verify_run_token(token)


def test_http_routes(example_payload, tmp_path):
    spec_text = eval_spec_text(example_payload)
    service, _, _ = build_service(spec_text, tmp_path)
    client = create_app(service, poll_seconds=0.0).test_client()
    saved = client.post("/v1/specs", data=spec_text)
    assert saved.status_code == 201
    digest = saved.get_json()["digest"]
    assert client.post("/v1/specs", data="experiment: {}").get_json()["code"] == "invalid_spec"
    assert client.get(f"/v1/specs/{digest}").get_json()["yaml"] == spec_text
    assert client.get("/v1/specs/sha256:missing").status_code == 404
    assert client.post(f"/v1/specs/{digest}/preflight").get_json()["is_valid"]
    launched = client.post(f"/v1/specs/{digest}/launch", json={})
    assert launched.status_code == 201
    run_id = launched.get_json()["run_ids"][0]
    rejected = client.post(f"/v1/runs/{run_id}/control", json={"command": "set_rubric", "value": "x"})
    assert rejected.status_code == 409
    client.post(f"/v1/runs/{run_id}/control", json={"command": "pause"})
    stream = client.get(f"/v1/runs/{run_id}/events?follow=false&after=0").get_data(as_text=True)
    frames = [frame for frame in stream.split("\n\n") if frame]
    assert frames[0].startswith("id: 1\nevent: launched\ndata: ")
    assert json.loads(frames[1].split("data: ", 1)[1])["detail"]["command"] == "pause"
    token = service.issue_run_token(run_id)
    assert client.post("/v1/run_tokens/verify", json={"token": token}).get_json()["run_id"] == run_id
    assert client.post("/v1/run_tokens/verify", json={"token": "forged.1.abc"}).status_code == 401
    assert len(client.get("/v1/runs?experiment_id=experiment_000001").get_json()["runs"]) == 6
    assert client.get("/healthz").get_json() == {"status": "ok"}


class FinishOnSecondPoll:
    """Stands in for sleep; the run completes while the stream is polling."""

    def __init__(self, service, run_id):
        self.service = service
        self.run_id = run_id
        self.polls = []

    def __call__(self, seconds):
        self.polls.append(seconds)
        if len(self.polls) == 2:
            self.service.mark_finished(self.run_id, "completed")


def test_following_the_stream_ends_with_the_run(example_payload, tmp_path):
    spec_text = eval_spec_text(example_payload)
    service, _, _ = build_service(spec_text, tmp_path)
    run_id = service.launch(service.save_spec(spec_text)).run_ids[0]
    sleeper = FinishOnSecondPoll(service, run_id)
    client = create_app(service, poll_seconds=0.5, sleep=sleeper).test_client()
    stream = client.get(f"/v1/runs/{run_id}/events").get_data(as_text=True)
    assert "event: completed" in stream
    assert sleeper.polls == [0.5, 0.5]
