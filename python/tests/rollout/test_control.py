import httpx
import pytest
from support.experiment_fakes import bridged_http_client, build_experiment_service, eval_payload

from rlworld.rollout import ExperimentdClient, RunControlError, connection_from_environment
from rlworld.specs import dump_spec_document, parse_spec_document

ENVIRONMENT = {
    "RLWORLD_RUN_ID": "experiment_000001_run_000",
    "RLWORLD_FLEET_URL": "http://fleetd.internal",
    "RLWORLD_EXPERIMENTD_URL": "http://experimentd.internal",
    "RLWORLD_POLICY_ENDPOINT": "http://vllm.internal/v1",
}


class Unreachable:
    def __call__(self, request):
        raise httpx.ConnectError("connection refused", request=request)


def launched_service(example_payload, tmp_path):
    payload = eval_payload(example_payload)
    service = build_experiment_service(payload, tmp_path)
    experiment = service.launch(service.save_spec(dump_spec_document(parse_spec_document(payload))))
    return service, experiment.run_ids


def client_for(service, token):
    return ExperimentdClient("http://experimentd.internal", token, bridged_http_client(service))


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
