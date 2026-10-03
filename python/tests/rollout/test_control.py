import copy

import httpx
import pytest

from rlworld.preflight import PreflightContext, SeatPool
from rlworld.rollout import ExperimentdClient, RunControlError, connection_from_environment
from rlworld.services.experimentd_http import create_app
from rlworld.services.experiments import ExperimentService
from rlworld.services.trainer_launch import TrainerLauncher
from rlworld.specs import dump_spec_document, expand_sweep, parse_spec_document, spec_digest
from rlworld.tasks import GateReport

ENVIRONMENT = {
    "RLWORLD_RUN_ID": "experiment_000001_run_000",
    "RLWORLD_FLEET_URL": "http://fleetd.internal",
    "RLWORLD_EXPERIMENTD_URL": "http://experimentd.internal",
    "RLWORLD_POLICY_ENDPOINT": "http://vllm.internal/v1",
}


class FlaskBridge:
    """httpx transport handler that serves each request from the experimentd Flask app."""

    def __init__(self, app):
        self.client = app.test_client()

    def __call__(self, request):
        headers = {name: value for name, value in request.headers.items()
                   if name.lower() in ("authorization", "content-type")}
        response = self.client.open(request.url.raw_path.decode(), method=request.method, data=request.content,
                                    headers=headers)
        return httpx.Response(response.status_code, content=response.get_data(),
                              headers={"Content-Type": response.content_type or "application/json"})


class IdleExecutor:
    def start(self, run, document):
        return None

    def apply(self, run):
        return None

    def dry_run(self, document):
        return "episode_dry_000001"


class Unreachable:
    def __call__(self, request):
        raise httpx.ConnectError("connection refused", request=request)


def launched_service(example_payload, tmp_path):
    payload = copy.deepcopy(example_payload)
    payload["run"].update({"type": "eval", "policy": {**payload["run"]["policy"], "token_ids": False,
                                                       "logprobs": False}})
    del payload["run"]["trainer"]
    payload["task"]["split"] = "eval"
    document = parse_spec_document(payload)
    gate_reports = {spec_digest(point.document.task): GateReport(spec_digest(point.document.task), ())
                    for point in expand_sweep(document)}
    context = PreflightContext(gate_reports=gate_reports, seat_pools={"windows_server_dc": SeatPool(1000, "per_host"),
                                                                      "office_ltsc": SeatPool(10_000)})
    service = ExperimentService(context, IdleExecutor(), TrainerLauncher({}, None, tmp_path), b"run token secret",
                                "http://fleetd.internal", "http://experimentd.internal")
    experiment = service.launch(service.save_spec(dump_spec_document(document)))
    return service, experiment.run_ids


def client_for(service, token):
    http_client = httpx.Client(transport=httpx.MockTransport(FlaskBridge(create_app(service, poll_seconds=0.0))))
    return ExperimentdClient("http://experimentd.internal", token, http_client)


def test_workers_read_their_spec_and_report_with_a_scoped_token(example_payload, tmp_path):
    service, run_ids = launched_service(example_payload, tmp_path)
    run_id, sibling_id = run_ids[3], run_ids[0]
    client = client_for(service, service.issue_run_token(run_id))
    assert client.verify_token() == run_id
    document = client.run_document(run_id)
    assert document == service.run_document(run_id)
    assert document.task.difficulty.distractors == service.run(run_id).assignments["task.difficulty.distractors"]
    assert client.run(run_id).state == "running"
    client.publish_metrics(run_id, {"episodes": 4})
    client.record_spend(run_id, 1.5)
    assert service.run_events(run_id)[-2].detail == {"episodes": 4}
    assert service.experiment(service.run(run_id).experiment_id).spent_usd == 1.5
    with pytest.raises(RunControlError, match="401"):
        client.publish_metrics(sibling_id, {"episodes": 1})
    with pytest.raises(RunControlError, match="401"):
        client_for(service, "").record_spend(run_id, 99.0)
    with pytest.raises(RunControlError, match="409"):
        client.mark_finished(run_id, "stopped")
    client.mark_finished(run_id, "completed", "all batches played")
    assert service.run(run_id).state == "completed"
    with pytest.raises(RunControlError, match="401"):
        client.publish_metrics(run_id, {"episodes": 5})


def test_unreachable_experimentd_is_a_control_error():
    client = ExperimentdClient("http://experimentd.internal", "token",
                               httpx.Client(transport=httpx.MockTransport(Unreachable())))
    with pytest.raises(RunControlError, match="unreachable"):
        client.run("experiment_000001_run_000")


def test_trainer_jobs_find_their_connection_in_the_environment(tmp_path):
    connection = connection_from_environment({**ENVIRONMENT, "RLWORLD_RUN_TOKEN": "token_abc"})
    assert (connection.run_id, connection.run_token) == ("experiment_000001_run_000", "token_abc")
    assert connection.fleet().base_url == "http://fleetd.internal"
    token_file = tmp_path / "run.token"
    token_file.write_text("token_from_file\n")
    from_file = connection_from_environment({**ENVIRONMENT, "RLWORLD_RUN_TOKEN_FILE": str(token_file)})
    assert from_file.run_token == "token_from_file"
    with pytest.raises(RunControlError, match="RLWORLD_FLEET_URL, RLWORLD_RUN_TOKEN or RLWORLD_RUN_TOKEN_FILE"):
        connection_from_environment({key: value for key, value in ENVIRONMENT.items() if key != "RLWORLD_FLEET_URL"})
