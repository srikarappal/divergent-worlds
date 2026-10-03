from pathlib import Path

import pytest

from rlworld.judge import RubricLibrary
from rlworld.preflight import PreflightContext, ProbeResult, SeatPool, StaticEndpointProbe
from rlworld.rewards import CheckResult, FunctionVerifier, VerifierRegistry
from rlworld.specs import expand_sweep, parse_spec_document, set_path
from rlworld.specs.digest import spec_digest
from rlworld.tasks import GateReport, GateResult

EXAMPLES_DIRECTORY = Path(__file__).resolve().parents[3] / "examples"
GATE_NAMES = ("null_action", "oracle", "rerun_stability", "alternative_solution", "near_miss", "solvability")
POLICY_URL = "http://vllm.internal/v1"


def noop_exact(context):
    return [CheckResult(name="noop", tier="exact", passed=True)]


def noop_semantic(context):
    return [CheckResult(name="noop", tier="semantic", passed=True)]


def passing_gate_report(task) -> GateReport:
    return GateReport(task_digest=spec_digest(task), results=tuple(GateResult(name, True, "") for name in GATE_NAMES))


def worked_example_payload(example_payload):
    """DESIGN.md section 8: Linux envs, 512 tasks x 8 rollouts, 25 steps of 3 s model plus 1 s env."""
    payload = set_path(example_payload, "image.os", "linux")
    payload = set_path(payload, "image.pool", {"vmm": "firecracker", "cpu_model": "fleet_baseline"})
    payload = set_path(payload, "image.licenses", [])
    return set_path(payload, "task.difficulty.oracle_steps", 25)


@pytest.fixture
def example_document(example_payload):
    return parse_spec_document(example_payload)


@pytest.fixture
def worked_example_raw(example_payload):
    return worked_example_payload(example_payload)


@pytest.fixture
def worked_example_document(worked_example_raw):
    return parse_spec_document(worked_example_raw)


@pytest.fixture
def verifier_registry():
    registry = VerifierRegistry()
    registry.register(FunctionVerifier("verifiers.excel.reconciliation_values_match", "1", "exact", noop_exact))
    registry.register(
        FunctionVerifier("verifiers.mail.summary_lists_discrepant_invoices", "1", "semantic", noop_semantic)
    )
    return registry


@pytest.fixture
def endpoint_probe():
    return StaticEndpointProbe(
        {
            POLICY_URL: ProbeResult(reachable=True, returns_token_ids=True, returns_logprobs=True,
                                    provider_kind="self_hosted"),
            "frontier": ProbeResult(reachable=True, provider_kind="frontier", rate_limit_tokens_per_minute=2_000_000),
        }
    )


@pytest.fixture
def preflight_context(example_document, verifier_registry, endpoint_probe):
    gate_reports = {
        spec_digest(point.document.task): passing_gate_report(point.document.task)
        for point in expand_sweep(example_document)
    }
    gate_reports[spec_digest(example_document.task)] = passing_gate_report(example_document.task)
    return PreflightContext(
        verifier_registry=verifier_registry,
        rubric_library=RubricLibrary(EXAMPLES_DIRECTORY),
        gate_reports=gate_reports,
        seat_pools={"windows_server_dc": SeatPool(1000, "per_host"), "office_ltsc": SeatPool(10_000)},
        identity_pool_size=50_000,
        endpoint_probe=endpoint_probe,
    )
