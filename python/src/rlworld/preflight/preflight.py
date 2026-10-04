"""Preflight: validate a run, probe its endpoints, estimate it and warn about known traps (DESIGN.md 6.14).

Errors block the launch; warnings explain what will be slower, costlier or
noisier than the researcher probably expects.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from rlworld.judge.rubric import RubricError, RubricLibrary
from rlworld.preflight.capacity import (
    DEFAULT_POOL_PROFILES,
    CapacityEstimate,
    JudgeProfile,
    PolicyProfile,
    PoolProfile,
    StorageProfile,
    estimate_capacity,
)
from rlworld.preflight.probes import EndpointProbe, ProbeResult
from rlworld.rewards.verifiers import UnknownVerifierError, VerifierRegistry
from rlworld.specs.digest import run_spec_digest, spec_digest
from rlworld.specs.document import SpecDocument, SpecValidationError, parse_spec_document
from rlworld.specs.sweep import expand_sweep
from rlworld.tasks.gates import GateReport
from rlworld.tasks.splits import HoldoutPolicy, holdout_problems

BROWSER_APPS = frozenset({"chrome", "chromium", "firefox", "edge", "webkit", "safari"})


def default_pool_profiles() -> dict[str, PoolProfile]:
    return dict(DEFAULT_POOL_PROFILES)


@dataclass(frozen=True)
class SeatPool:
    available: int
    scope: Literal["per_env", "per_host"] = "per_env"


@dataclass(frozen=True)
class PreflightContext:
    verifier_registry: VerifierRegistry | None = None
    rubric_library: RubricLibrary | None = None
    gate_reports: Mapping[str, GateReport] = field(default_factory=dict)
    seat_pools: Mapping[str, SeatPool] = field(default_factory=dict)
    identity_pool_size: int | None = None
    tenant_max_concurrent_envs: int | None = None
    available_hosts_by_pool: Mapping[str, int] = field(default_factory=dict)
    endpoint_probe: EndpointProbe | None = None
    frontier_endpoints: frozenset[str] = frozenset({"frontier"})
    policy_profile: PolicyProfile = field(default_factory=PolicyProfile)
    judge_profile: JudgeProfile = field(default_factory=JudgeProfile)
    pool_profiles: Mapping[str, PoolProfile] = field(default_factory=default_pool_profiles)
    storage_profile: StorageProfile = field(default_factory=StorageProfile)
    holdout: HoldoutPolicy = field(default_factory=HoldoutPolicy)


class PreflightIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str


class PreflightReport(BaseModel):
    is_valid: bool
    issues: list[PreflightIssue]
    run_digest: str | None = None
    estimated_concurrent_envs: int = 0
    estimated_hosts_by_pool: dict[str, int] = {}
    estimated_policy_gpus: int = 0
    estimated_judge_tokens: int = 0
    estimated_cost_usd: float | None = None
    estimated_wall_clock_hours: float = 0.0
    estimate: dict[str, Any] | None = None

    @property
    def errors(self) -> list[str]:
        return [f"{issue.code}: {issue.message}" for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [f"{issue.code}: {issue.message}" for issue in self.issues if issue.severity == "warning"]

    def codes(self, severity: str) -> list[str]:
        return [issue.code for issue in self.issues if issue.severity == severity]


class ExperimentPreflight(BaseModel):
    is_valid: bool
    points: dict[str, PreflightReport]
    estimated_cost_usd: float | None
    budget_usd: float
    errors: list[str]


class IssueCollector:
    def __init__(self) -> None:
        self.issues: list[PreflightIssue] = []

    def error(self, code: str, message: str) -> None:
        self.issues.append(PreflightIssue(severity="error", code=code, message=message))

    def warning(self, code: str, message: str) -> None:
        self.issues.append(PreflightIssue(severity="warning", code=code, message=message))


def check_verifiers_and_rubric(document: SpecDocument, context: PreflightContext, collector: IssueCollector) -> None:
    task = document.task
    if context.verifier_registry is not None:
        for tier, names in (("exact", task.verifiers.exact), ("semantic", task.verifiers.semantic)):
            for name in names:
                try:
                    context.verifier_registry.resolve([name], tier)
                except UnknownVerifierError:
                    collector.error("unknown_verifier", f"verifier {name} is not registered")
                except ValueError as error:
                    collector.error("verifier_tier", str(error))
    if context.rubric_library is not None and task.judge_rubric:
        try:
            context.rubric_library.load(task.judge_rubric)
        except RubricError as error:
            collector.error("missing_rubric", str(error))


def check_gates(document: SpecDocument, context: PreflightContext, collector: IssueCollector) -> None:
    gate_report = context.gate_reports.get(spec_digest(document.task))
    report_problem = collector.error if document.run.type == "rl" else collector.warning
    if gate_report is None:
        report_problem("ungated_task", "this task has no validation gate report yet")
        return
    if not gate_report.passed:
        report_problem("gates_failed", "; ".join(gate_report.review_reasons))
        if not gate_report.result("alternative_solution").passed:
            collector.warning(
                "false_negative_risk",
                "no alternative solution passed the verifiers, so valid work may be scored as failure",
            )


def check_holdout(document: SpecDocument, context: PreflightContext, collector: IssueCollector) -> None:
    """Training may not touch the apps, sites, templates and world seeds held out for eval."""
    for problem in holdout_problems(document, context.holdout):
        collector.error("holdout_overlap", problem)


def check_image_fits_task(document: SpecDocument, collector: IssueCollector) -> None:
    needs_browser = "dom" in document.task.observation or "browser" in document.task.allowed_actions
    if needs_browser and not BROWSER_APPS.intersection(document.image.apps):
        collector.error("no_browser", "the task uses DOM observations or browser actions but the image has no browser")


def check_endpoints(
    document: SpecDocument, context: PreflightContext, collector: IssueCollector
) -> tuple[ProbeResult | None, ProbeResult | None]:
    run = document.run
    policy_is_frontier = run.policy.endpoint in context.frontier_endpoints
    if run.type == "rl" and policy_is_frontier:
        collector.error("frontier_rl", "frontier endpoints serve eval and data runs; rl needs your own policy servers")
    if context.endpoint_probe is None:
        collector.warning("endpoints_unprobed", "endpoints were not probed")
        return None, None
    policy_probe = context.endpoint_probe.probe(run.policy)
    if not policy_probe.reachable:
        collector.error("policy_unreachable", policy_probe.error or "the policy endpoint did not answer")
    elif run.type == "rl" and not (policy_probe.returns_token_ids and policy_probe.returns_logprobs):
        collector.error(
            "rl_needs_token_ids",
            "the policy endpoint does not return token ids and logprobs, which on policy RL needs",
        )
    judge_probe = None
    if run.judge is not None:
        judge_probe = context.endpoint_probe.probe(run.judge)
        if not judge_probe.reachable:
            collector.error("judge_unreachable", judge_probe.error or "the judge endpoint did not answer")
    return policy_probe, judge_probe


def concurrency_cap(document: SpecDocument, context: PreflightContext, collector: IssueCollector) -> int:
    """The concurrency the fleet, the quota and the seat pools actually allow."""
    run = document.run
    pool_name = document.image.pool.vmm
    vms_per_host = context.pool_profiles[pool_name].vms_per_host
    wanted = min(run.max_concurrent_envs, run.tasks_per_batch * run.group_size)
    allowed = wanted
    if context.tenant_max_concurrent_envs is not None and context.tenant_max_concurrent_envs < allowed:
        allowed = context.tenant_max_concurrent_envs
        collector.warning("tenant_quota", f"the tenant quota caps concurrency at {allowed} envs")
    available_hosts = context.available_hosts_by_pool.get(pool_name)
    if available_hosts is not None and available_hosts * vms_per_host < allowed:
        allowed = available_hosts * vms_per_host
        collector.warning("fleet_capacity", f"{available_hosts} {pool_name} hosts cap concurrency at {allowed} envs")
    for license_name in document.image.licenses:
        seat_pool = context.seat_pools.get(license_name)
        if seat_pool is None:
            collector.error("unknown_license_pool", f"no seat pool is configured for {license_name}")
            continue
        seat_limited_envs = seat_pool.available * (vms_per_host if seat_pool.scope == "per_host" else 1)
        if seat_limited_envs < allowed:
            allowed = seat_limited_envs
            collector.warning(
                "license_seats",
                f"the {license_name} pool has {seat_pool.available} seats, which caps concurrency at {allowed} envs",
            )
    if document.world.identity is not None and context.identity_pool_size is not None:
        if context.identity_pool_size < allowed:
            allowed = context.identity_pool_size
            collector.warning("identity_pool", f"{allowed} synthetic identities cap concurrency at {allowed} envs")
    if allowed < 1:
        collector.error("no_capacity", "no envs can run with the current quota, hosts and seats")
    return max(1, allowed)


def warn_about_traps(
    document: SpecDocument,
    estimate: CapacityEstimate,
    policy_probe: ProbeResult | None,
    collector: IssueCollector,
) -> None:
    run = document.run
    if estimate.frontier_input_tokens_per_minute and policy_probe and policy_probe.rate_limit_tokens_per_minute:
        if estimate.frontier_input_tokens_per_minute > policy_probe.rate_limit_tokens_per_minute:
            collector.warning(
                "frontier_rate_limit",
                f"the run needs about {estimate.frontier_input_tokens_per_minute:,.0f} input tokens per minute but "
                f"the endpoint allows {policy_probe.rate_limit_tokens_per_minute:,}; envs will idle behind it",
            )
    if run.history == "sliding_window" and estimate.policy_gpus:
        collector.warning(
            "prefix_cache",
            f"a sliding screenshot window defeats prefix caching: about {estimate.policy_gpus_if_cache_breaks} "
            f"policy GPUs instead of {estimate.policy_gpus}",
        )
    if run.type == "rl" and run.group_size < 2:
        collector.warning("group_size", "GRPO needs at least two rollouts per task to compute an advantage")
    if document.world.network.internet in ("allowlist", "live"):
        collector.warning("live_internet", "the live web changes under the run, so rewards will drift")
    if estimate.missing_prices:
        collector.warning("missing_prices", f"no price configured for {', '.join(estimate.missing_prices)}")


def run_preflight(document: SpecDocument, context: PreflightContext | None = None) -> PreflightReport:
    preflight_context = context or PreflightContext()
    collector = IssueCollector()
    check_verifiers_and_rubric(document, preflight_context, collector)
    check_gates(document, preflight_context, collector)
    check_holdout(document, preflight_context, collector)
    check_image_fits_task(document, collector)
    policy_probe, _ = check_endpoints(document, preflight_context, collector)
    estimate = estimate_capacity(
        document,
        preflight_context.policy_profile,
        preflight_context.judge_profile,
        preflight_context.pool_profiles,
        preflight_context.storage_profile,
        policy_is_frontier=document.run.policy.endpoint in preflight_context.frontier_endpoints,
        concurrency_cap=concurrency_cap(document, preflight_context, collector),
    )
    warn_about_traps(document, estimate, policy_probe, collector)
    estimated_cost = estimate.estimated_cost_usd
    if estimated_cost is not None and estimated_cost > document.experiment.budget_usd:
        collector.error(
            "over_budget",
            f"estimated cost {estimated_cost:,.2f} USD exceeds the {document.experiment.budget_usd:,.2f} USD budget",
        )
    estimate_payload = {**asdict(estimate), "estimated_cost_usd": estimated_cost}
    return PreflightReport(
        is_valid=not any(issue.severity == "error" for issue in collector.issues),
        issues=collector.issues,
        run_digest=run_spec_digest(document),
        estimated_concurrent_envs=estimate.concurrent_envs,
        estimated_hosts_by_pool=estimate.hosts_by_pool,
        estimated_policy_gpus=estimate.policy_gpus,
        estimated_judge_tokens=estimate.judge_input_tokens,
        estimated_cost_usd=estimated_cost,
        estimated_wall_clock_hours=round(estimate.wall_clock_hours, 3),
        estimate=estimate_payload,
    )


def run_preflight_payload(raw_document: dict[str, Any], context: PreflightContext | None = None) -> PreflightReport:
    """Preflight straight from the console's form payload; spec problems come back as errors."""
    try:
        document = parse_spec_document(raw_document)
    except SpecValidationError as error:
        issues = [PreflightIssue(severity="error", code="spec", message=problem) for problem in error.problems]
        return PreflightReport(is_valid=False, issues=issues)
    return run_preflight(document, context)


def run_experiment_preflight(document: SpecDocument, context: PreflightContext | None = None) -> ExperimentPreflight:
    """Preflight every sweep point, then check the summed cost against the experiment budget."""
    try:
        sweep_points = expand_sweep(document)
    except SpecValidationError as error:
        return ExperimentPreflight(
            is_valid=False, points={}, estimated_cost_usd=None, budget_usd=document.experiment.budget_usd,
            errors=error.problems,
        )
    points = {point.run_name: run_preflight(point.document, context) for point in sweep_points}
    point_costs = [report.estimated_cost_usd for report in points.values()]
    total_cost = None if any(cost is None for cost in point_costs) else round(sum(point_costs), 2)
    errors = [f"{run_name}: {error}" for run_name, report in points.items() for error in report.errors]
    if total_cost is not None and total_cost > document.experiment.budget_usd:
        errors.append(f"experiment: estimated cost {total_cost:,.2f} USD exceeds the budget")
    return ExperimentPreflight(
        is_valid=not errors,
        points=points,
        estimated_cost_usd=total_cost,
        budget_usd=document.experiment.budget_usd,
        errors=errors,
    )
