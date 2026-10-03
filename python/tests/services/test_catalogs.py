from pathlib import Path

import pytest
from support.experiment_fakes import build_experiment_service, eval_payload

from rlworld.gateway import EndpointConfig, EndpointRegistry, StaticSecretStore
from rlworld.judge.rubric import RubricLibrary
from rlworld.preflight.probes import ProbeResult, StaticEndpointProbe
from rlworld.rewards import CheckResult, FunctionVerifier, VerifierRegistry
from rlworld.services.catalogs import (
    CatalogEntry,
    CatalogService,
    CatalogWriteError,
    EndpointService,
    UnknownCatalogError,
)
from rlworld.services.experimentd_http import create_app
from rlworld.specs import dump_spec_document, parse_spec_document

EXAMPLES_DIRECTORY = Path(__file__).resolve().parents[3] / "examples"
BYO_JUDGE = {"name": "byo_judge", "base_url": "https://judge.customer.example/v1", "allowed_models": ["judge_v1"],
             "api_key_secret": "byo_judge_key", "provider_kind": "user"}


def no_checks(context):
    return [CheckResult(name="placeholder", tier="exact", passed=True)]


def verifier_registry():
    registry = VerifierRegistry()
    for name, tier in (("verifiers.excel.reconciliation_values_match", "exact"),
                       ("verifiers.mail.summary_lists_discrepant_invoices", "semantic"),
                       ("verifiers.crm.deal_stage_matches", "exact")):
        registry.register(FunctionVerifier(name, "2", tier, no_checks))
    return registry


def endpoint_registry():
    return EndpointRegistry([
        EndpointConfig(name="policy_vllm", base_url="http://vllm.internal/v1", allowed_models=("my_policy",),
                       provider_kind="self_hosted", returns_token_ids=True),
        EndpointConfig(name="frontier", base_url="https://api.frontier.example/v1",
                       allowed_models=("frontier_judge_2026_06",), api_key_secret="frontier_key",
                       provider_kind="frontier"),
    ])


@pytest.fixture
def launched(example_payload, tmp_path):
    payload = eval_payload(example_payload)
    service = build_experiment_service(payload, tmp_path)
    service.launch(service.save_spec(dump_spec_document(parse_spec_document(payload))))
    catalogs = CatalogService(service.documents, verifier_registry(), RubricLibrary(EXAMPLES_DIRECTORY),
                              endpoint_registry())
    return service, catalogs


def test_catalogs_count_the_runs_that_use_each_entry(launched):
    _, catalogs = launched
    catalogs.publish(CatalogEntry(kind="images", name="win2025_office_v4", version="sha256:4f9c", clone_fidelity=0.93))
    catalogs.publish(CatalogEntry(kind="task_templates", name="tasks.finance.reconcile_invoices", version="3",
                                  gate_status="passed", pass_rates={"my_policy@3": 0.42}))
    image = catalogs.entries("images")[0]
    assert (image.usage_runs, image.clone_fidelity) == (6, 0.93)
    assert catalogs.entries("task_templates")[0].pass_rates == {"my_policy@3": 0.42}
    usage_by_verifier = {entry.name: entry.usage_runs for entry in catalogs.entries("verifiers")}
    assert usage_by_verifier == {"verifiers.crm.deal_stage_matches": 0,
                                 "verifiers.excel.reconciliation_values_match": 6,
                                 "verifiers.mail.summary_lists_discrepant_invoices": 6}
    rubric = catalogs.entries("rubrics")[0]
    assert rubric.name == "rubrics/summary_email_quality.md" and rubric.usage_runs == 6 and "+" in rubric.version
    usage_by_endpoint = {entry.name: entry.usage_runs for entry in catalogs.entries("endpoints")}
    assert usage_by_endpoint == {"frontier": 6, "policy_vllm": 6}
    assert catalogs.entries("worlds") == []


def test_registry_backed_catalogs_are_read_only_and_kinds_are_checked(launched):
    _, catalogs = launched
    with pytest.raises(CatalogWriteError):
        catalogs.publish(CatalogEntry(kind="verifiers", name="verifiers.fake", version="1"))
    with pytest.raises(UnknownCatalogError):
        catalogs.entries("models")


def endpoint_service():
    vault = StaticSecretStore({})
    probe = StaticEndpointProbe({"byo_judge": ProbeResult(reachable=True, latency_seconds=0.2, provider_kind="user")})
    return EndpointService(EndpointRegistry([]), vault, probe), vault


def test_endpoint_routes_register_probe_and_take_keys_write_only(launched):
    service, catalogs = launched
    endpoints, vault = endpoint_service()
    client = create_app(service, catalog_service=catalogs, endpoint_service=endpoints).test_client()
    registered = client.post("/v1/endpoints", json=BYO_JUDGE)
    assert registered.status_code == 201 and registered.get_json()["has_key"] is False
    assert client.post("/v1/endpoints", json=BYO_JUDGE).status_code == 409
    moving_alias = client.post("/v1/endpoints", json={**BYO_JUDGE, "name": "other", "allowed_models": ["judge_latest"]})
    assert moving_alias.status_code == 400 and "pin exact versions" in moving_alias.get_json()["problems"][0]
    assert client.put("/v1/endpoints/byo_judge/key", json={"key": "sk_customer_123"}).status_code == 204
    assert vault.get("byo_judge_key") == "sk_customer_123"
    listed = client.get("/v1/endpoints")
    assert listed.get_json()["endpoints"][0]["has_key"] is True
    assert "sk_customer_123" not in listed.get_data(as_text=True)
    assert client.post("/v1/endpoints/byo_judge/test").get_json()["reachable"] is True
    assert client.put("/v1/endpoints/missing/key", json={"key": "x"}).status_code == 404
    keyless = {**BYO_JUDGE, "name": "internal_judge", "api_key_secret": None}
    client.post("/v1/endpoints", json=keyless)
    assert client.put("/v1/endpoints/internal_judge/key", json={"key": "x"}).status_code == 409


def test_catalog_routes_list_publish_and_validate(launched):
    service, catalogs = launched
    client = create_app(service, catalog_service=catalogs).test_client()
    published = client.put("/v1/catalogs/images/win2025_office_v4", json={"version": "sha256:4f9c"})
    assert published.status_code == 200
    assert client.get("/v1/catalogs/images").get_json()["entries"][0]["usage_runs"] == 6
    assert client.get("/v1/catalogs/models").status_code == 404
    assert client.put("/v1/catalogs/verifiers/verifiers.fake", json={"version": "1"}).status_code == 409
    invalid = client.put("/v1/catalogs/images/win2025_office_v4", json={"version": "1", "clone_fidelity": 2.0})
    assert invalid.status_code == 400 and invalid.get_json()["problems"][0].startswith("clone_fidelity")
    assert create_app(service).test_client().get("/v1/catalogs/images").status_code == 503
