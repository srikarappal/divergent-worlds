import dataclasses

from rlworld.judge import RubricLibrary
from rlworld.preflight import (
    JudgeProfile,
    PolicyProfile,
    PoolProfile,
    ProbeResult,
    SeatPool,
    StaticEndpointProbe,
    StorageProfile,
    run_experiment_preflight,
    run_preflight,
    run_preflight_payload,
)
from rlworld.specs import expand_sweep, parse_spec_document, set_path
from rlworld.specs.digest import spec_digest
from rlworld.tasks import GateReport, GateResult
from rlworld.tasks.splits import HoldoutPolicy

FULL_PRICES = {
    "policy_profile": PolicyProfile(gpu_hourly_cost_usd=2.0),
    "judge_profile": JudgeProfile(input_price_per_million_usd=1.0, output_price_per_million_usd=2.0),
    "pool_profiles": {"qemu_templated": PoolProfile(vms_per_host=100, env_step_seconds=1.5, host_hourly_cost_usd=5.0)},
    "storage_profile": StorageProfile(gigabyte_month_price_usd=0.02),
}


def eval_payload(example_payload):
    payload = set_path(example_payload, "run.type", "eval")
    payload = set_path(payload, "run.trainer", None)
    return set_path(payload, "run.priority", "eval")


def test_the_example_run_passes_with_an_estimate(example_document, preflight_context):
    report = run_preflight(example_document, preflight_context)
    assert report.is_valid, report.errors
    assert report.estimated_concurrent_envs == 4000
    assert report.estimated_hosts_by_pool == {"qemu_templated": 40}
    assert report.estimated_policy_gpus == 92
    assert report.estimated_cost_usd is None
    assert "missing_prices" in report.codes("warning")
    assert report.run_digest.startswith("sha256:")
    assert report.estimate["policy_calls_per_second"] > 0


def test_unknown_verifiers_and_rubrics_block(example_document, preflight_context, tmp_path):
    preflight_context.verifier_registry.verifiers.pop("verifiers.excel.reconciliation_values_match")
    context = dataclasses.replace(preflight_context, rubric_library=RubricLibrary(tmp_path))
    report = run_preflight(example_document, context)
    assert not report.is_valid
    assert {"unknown_verifier", "missing_rubric"} <= set(report.codes("error"))


def test_ungated_tasks_block_rl_but_only_warn_for_eval(example_payload, preflight_context):
    context = dataclasses.replace(preflight_context, gate_reports={})
    assert "ungated_task" in run_preflight(parse_spec_document(example_payload), context).codes("error")
    eval_report = run_preflight(parse_spec_document(eval_payload(example_payload)), context)
    assert "ungated_task" in eval_report.codes("warning")
    assert eval_report.is_valid


def test_failed_gates_and_false_negative_risk(example_document, preflight_context):
    failing_results = tuple(
        GateResult(name, name != "alternative_solution", "the verifiers reject valid solutions: x")
        for name in ("null_action", "oracle", "rerun_stability", "alternative_solution", "near_miss", "solvability")
    )
    gate_reports = {spec_digest(example_document.task): GateReport(spec_digest(example_document.task), failing_results)}
    report = run_preflight(example_document, dataclasses.replace(preflight_context, gate_reports=gate_reports))
    assert "gates_failed" in report.codes("error")
    assert "false_negative_risk" in report.codes("warning")


def test_dom_observations_need_a_browser(example_payload, preflight_context):
    payload = set_path(example_payload, "image.apps", {"excel": "office_ltsc_2024"})
    payload = set_path(payload, "task.observation", ["screenshot", "dom"])
    document = parse_spec_document(payload)
    gate_reports = {spec_digest(document.task): GateReport(spec_digest(document.task), ())}
    report = run_preflight(document, dataclasses.replace(preflight_context, gate_reports=gate_reports))
    assert "no_browser" in report.codes("error")


def test_endpoint_problems(example_payload, example_document, preflight_context):
    no_token_ids = StaticEndpointProbe(
        {"http://vllm.internal/v1": ProbeResult(reachable=True), "frontier": ProbeResult(reachable=False)}
    )
    report = run_preflight(example_document, dataclasses.replace(preflight_context, endpoint_probe=no_token_ids))
    assert {"rl_needs_token_ids", "judge_unreachable"} <= set(report.codes("error"))
    unreachable = StaticEndpointProbe({})
    report = run_preflight(example_document, dataclasses.replace(preflight_context, endpoint_probe=unreachable))
    assert "policy_unreachable" in report.codes("error")
    unprobed = run_preflight(example_document, dataclasses.replace(preflight_context, endpoint_probe=None))
    assert "endpoints_unprobed" in unprobed.codes("warning")


def test_frontier_policies(example_payload, preflight_context):
    rl_payload = set_path(example_payload, "run.policy.endpoint", "frontier")
    rl_report = run_preflight(parse_spec_document(rl_payload), preflight_context)
    assert "frontier_rl" in rl_report.codes("error")
    eval_frontier = set_path(eval_payload(example_payload), "run.policy.endpoint", "frontier")
    eval_report = run_preflight(parse_spec_document(eval_frontier), preflight_context)
    assert "frontier_rate_limit" in eval_report.codes("warning")
    assert eval_report.estimated_policy_gpus == 0


def test_seats_quota_hosts_and_identities_cap_concurrency(example_document, preflight_context):
    context = dataclasses.replace(
        preflight_context,
        seat_pools={"windows_server_dc": SeatPool(30, "per_host"), "office_ltsc": SeatPool(2500)},
        tenant_max_concurrent_envs=3500,
        available_hosts_by_pool={"qemu_templated": 32},
        identity_pool_size=1200,
    )
    report = run_preflight(example_document, context)
    assert report.estimated_concurrent_envs == 1200
    assert {"tenant_quota", "fleet_capacity", "license_seats", "identity_pool"} <= set(report.codes("warning"))
    missing_pool = dataclasses.replace(preflight_context, seat_pools={"windows_server_dc": SeatPool(1000, "per_host")})
    assert "unknown_license_pool" in run_preflight(example_document, missing_pool).codes("error")


def test_training_on_what_eval_holds_out_is_blocked(example_document, preflight_context):
    holdout = HoldoutPolicy(templates={example_document.task.generator}, world_seeds={example_document.world.seed})
    held = run_preflight(example_document, dataclasses.replace(preflight_context, holdout=holdout))
    assert held.codes("error").count("holdout_overlap") == 2 and not held.is_valid
    assert "holdout_overlap" not in run_preflight(example_document, preflight_context).codes("error")


def test_live_internet_warns_that_rewards_drift(example_payload, preflight_context):
    payload = set_path(eval_payload(example_payload), "task.split", "eval")
    for mode, allowlist in (("allowlist", ["docs.vendor.example"]), ("live", [])):
        live = set_path(set_path(payload, "world.network.internet", mode), "world.network.allowlist", allowlist)
        assert "live_internet" in run_preflight(parse_spec_document(live), preflight_context).codes("warning")
    replayed = set_path(payload, "world.network.internet", "replay_only")
    assert "live_internet" not in run_preflight(parse_spec_document(replayed), preflight_context).codes("warning")


def test_sliding_window_history_warns_about_prefix_caching(example_payload, preflight_context):
    payload = set_path(example_payload, "run.history", "sliding_window")
    report = run_preflight(parse_spec_document(payload), preflight_context)
    assert "prefix_cache" in report.codes("warning")


def test_runs_over_budget_are_blocked(example_document, preflight_context):
    report = run_preflight(example_document, dataclasses.replace(preflight_context, **FULL_PRICES))
    assert report.estimated_cost_usd is not None
    assert report.estimated_cost_usd > example_document.experiment.budget_usd
    assert "over_budget" in report.codes("error")


def test_spec_errors_come_back_as_report_errors(example_payload, preflight_context):
    report = run_preflight_payload(set_path(example_payload, "run.group_size", 0), preflight_context)
    assert not report.is_valid
    assert report.codes("error") == ["spec"]
    assert report.errors[0].startswith("spec: run.group_size")


def test_experiment_preflight_covers_every_sweep_point(example_payload, example_document, preflight_context):
    unpriced = run_experiment_preflight(example_document, preflight_context)
    assert len(unpriced.points) == 6
    assert unpriced.estimated_cost_usd is None
    assert unpriced.is_valid, unpriced.errors
    cheap_payload = set_path(example_payload, "run.batches", 1)
    cheap_payload = set_path(cheap_payload, "experiment.budget_usd", 1_000_000)
    cheap_document = parse_spec_document(cheap_payload)
    gate_reports = dict(preflight_context.gate_reports)
    for point in expand_sweep(cheap_document):
        gate_reports[spec_digest(point.document.task)] = GateReport(spec_digest(point.document.task), ())
    priced_context = dataclasses.replace(preflight_context, gate_reports=gate_reports, **FULL_PRICES)
    priced = run_experiment_preflight(cheap_document, priced_context)
    assert priced.estimated_cost_usd == round(sum(report.estimated_cost_usd for report in priced.points.values()), 2)
    tight_budget = parse_spec_document(set_path(cheap_payload, "experiment.budget_usd", 1))
    assert not run_experiment_preflight(tight_budget, priced_context).is_valid
    invalid_sweep = parse_spec_document(set_path(example_payload, "experiment.sweep", {"run.group_size": [0]}))
    assert not run_experiment_preflight(invalid_sweep, preflight_context).is_valid
