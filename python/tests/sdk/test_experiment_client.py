import io
import json
import re
import shlex
from operator import itemgetter
from pathlib import Path

import pytest
from support.experiment_fakes import bridged_http_client, build_experiment_service, eval_payload

from rlworld.cli import HANDLERS, build_parser, main
from rlworld.gateway import EndpointRegistry, StaticSecretStore
from rlworld.preflight.probes import StaticEndpointProbe
from rlworld.sdk import ExperimentApiError, ExperimentClient
from rlworld.services.catalogs import CatalogService, EndpointService
from rlworld.services.experimentd_http import create_app
from rlworld.specs import dump_spec_document, load_spec_document, parse_spec_document

OPERATIONS_PATH = Path(__file__).resolve().parents[3] / "schemas" / "api_operations.json"
OPERATIONS = json.loads(OPERATIONS_PATH.read_text())["operations"]
PLACEHOLDER_PATTERN = re.compile(r"\{(\w+)\}")
ENDPOINT_YAML = """name: byo_judge
base_url: https://judge.customer.example/v1
allowed_models: [judge_v1]
api_key_secret: byo_judge_key
"""


@pytest.fixture
def spec_yaml(example_payload):
    return dump_spec_document(parse_spec_document(eval_payload(example_payload)))


@pytest.fixture
def experiment_service(example_payload, tmp_path):
    return build_experiment_service(eval_payload(example_payload), tmp_path)


def sdk_client(experiment_service):
    catalogs = CatalogService(experiment_service.documents)
    endpoints = EndpointService(EndpointRegistry([]), StaticSecretStore({}), StaticEndpointProbe({}))
    http_client = bridged_http_client(experiment_service, catalog_service=catalogs, endpoint_service=endpoints)
    return ExperimentClient("http://experimentd.internal", http_client)


def test_the_sdk_covers_the_console_flow(experiment_service, spec_yaml):
    client = sdk_client(experiment_service)
    digest = client.save_spec(spec_yaml)
    assert client.get_spec(digest) == spec_yaml
    report = client.preflight(digest)
    assert report.is_valid and len(report.points) == 6
    assert client.dry_run(digest) == "dry_run_000001_s000_a0"
    experiment = client.launch(digest, budget_usd=100.0)
    assert experiment.budget_usd == 100.0 and len(experiment.run_ids) == 6
    runs = client.list_runs(experiment_id=experiment.experiment_id)
    run_id = runs[0].run_id
    assert client.control_run(run_id, "pause").state == "paused"
    assert client.control_run(run_id, "set_concurrency", 100).desired_concurrency == 100
    assert client.get_run(run_id).desired_concurrency == 100
    assert load_spec_document(client.get_run_spec(run_id)) == experiment_service.run_document(run_id)
    assert [event.kind for event in client.run_events(run_id)] == ["launched", "control", "control"]
    assert client.list_catalog("images") == [] and client.list_endpoints() == []
    with pytest.raises(ExperimentApiError) as invalid:
        client.save_spec("experiment: {}")
    assert invalid.value.code == "invalid_spec" and invalid.value.problems
    with pytest.raises(ExperimentApiError) as rejected:
        client.control_run(run_id, "set_rubric", "rubrics/other.md")
    assert rejected.value.status_code == 409


def test_the_cli_drives_the_same_operations(experiment_service, spec_yaml, tmp_path, capsys, monkeypatch):
    client = sdk_client(experiment_service)
    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(spec_yaml)
    assert main(["specs", "save", str(spec_file)], client=client) == 0
    digest = json.loads(capsys.readouterr().out)["digest"]
    assert main(["specs", "launch", digest, "--budget-usd", "250"], client=client) == 0
    experiment = json.loads(capsys.readouterr().out)
    run_id = experiment["run_ids"][0]
    assert experiment["budget_usd"] == 250.0
    main(["runs", "control", run_id, "set_concurrency", "64"], client=client)
    assert json.loads(capsys.readouterr().out)["desired_concurrency"] == 64
    main(["runs", "control", run_id, "set_priority", "eval"], client=client)
    assert json.loads(capsys.readouterr().out)["priority"] == "eval"
    main(["runs", "events", run_id], client=client)
    assert [json.loads(line)["kind"] for line in capsys.readouterr().out.splitlines()] == [
        "launched", "control", "control"]
    endpoint_file = tmp_path / "endpoint.yaml"
    endpoint_file.write_text(ENDPOINT_YAML)
    main(["endpoints", "register", str(endpoint_file)], client=client)
    assert json.loads(capsys.readouterr().out)["has_key"] is False
    monkeypatch.setattr("sys.stdin", io.StringIO("sk_customer_123\n"))
    main(["endpoints", "set-key", "byo_judge"], client=client)
    assert "sk_customer_123" not in capsys.readouterr().out
    assert client.list_endpoints()[0]["has_key"] is True
    invalid_file = tmp_path / "invalid.yaml"
    invalid_file.write_text("experiment: {}\n")
    assert main(["specs", "save", str(invalid_file)], client=client) == 1
    assert "invalid_spec" in capsys.readouterr().err


@pytest.mark.parametrize("operation", OPERATIONS, ids=itemgetter("name"))
def test_every_console_operation_has_a_route_an_sdk_method_and_a_cli_command(operation, experiment_service):
    app = create_app(experiment_service)
    routes = {(method, rule.rule) for rule in app.url_map.iter_rules() for method in rule.methods}
    assert (operation["method"], PLACEHOLDER_PATTERN.sub(r"<\1>", operation["path"])) in routes
    assert callable(getattr(ExperimentClient, operation["name"], None))
    assert operation["sdk"].startswith(f"client.{operation['name']}(")
    words = shlex.split(PLACEHOLDER_PATTERN.sub("1", operation["cli"]))
    command_words = words[1:words.index("<")] if "<" in words else words[1:]
    assert words[0] == "rlworld"
    assert build_parser().parse_args(command_words).operation == operation["name"]
    assert operation["name"] in HANDLERS
