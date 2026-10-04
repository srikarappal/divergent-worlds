import json
import time
from pathlib import Path

import pytest
from support.experiment_fakes import build_experiment_service, eval_payload

from rlworld.analysis import clone_fidelity
from rlworld.entitlements import (
    EntitlementBroker,
    EntitlementRequest,
    EphemeralSecretIssuer,
    IdentityPoolConfig,
    SeatPoolConfig,
    SignedSessionMinter,
)
from rlworld.gateway.metering import CallRecord, UsageMeter
from rlworld.services.metrics import EpisodeMetrics, MetricsRegistry, PlatformMetrics
from rlworld.specs import dump_spec_document, parse_spec_document
from rlworld.trajectories.replay import DivergenceReport

CATALOG = Path(__file__).resolve().parents[3] / "schemas" / "metrics.json"


def test_counters_gauges_and_histograms_write_the_text_format():
    registry = MetricsRegistry()
    failures = registry.counter("rlworld_a_total", "Things\nthat failed", ("pool",))
    failures.inc("windows")
    failures.add(2.5, "linux")
    registry.gauge("rlworld_b", "A gauge").set(0.95)
    latency = registry.histogram("rlworld_c_seconds", "Latency", (1.0, 0.1), ("part",))
    for value in (0.05, 0.5, 3.0):
        latency.observe(value, 'say "hi"\\now')
    assert registry.render() == (
        "# HELP rlworld_a_total Things\\nthat failed\n# TYPE rlworld_a_total counter\n"
        'rlworld_a_total{pool="linux"} 2.5\nrlworld_a_total{pool="windows"} 1\n'
        "# HELP rlworld_b A gauge\n# TYPE rlworld_b gauge\nrlworld_b 0.95\n"
        "# HELP rlworld_c_seconds Latency\n# TYPE rlworld_c_seconds histogram\n"
        'rlworld_c_seconds_bucket{part="say \\"hi\\"\\\\now",le="0.1"} 1\n'
        'rlworld_c_seconds_bucket{part="say \\"hi\\"\\\\now",le="1"} 2\n'
        'rlworld_c_seconds_bucket{part="say \\"hi\\"\\\\now",le="+Inf"} 3\n'
        'rlworld_c_seconds_sum{part="say \\"hi\\"\\\\now"} 3.55\n'
        'rlworld_c_seconds_count{part="say \\"hi\\"\\\\now"} 3\n')


class Samples:
    def __init__(self):
        self.samples = []

    def collect(self):
        return self.samples


def test_computed_gauges_are_collected_at_every_scrape():
    registry = MetricsRegistry()
    source = Samples()
    registry.gauge_function("rlworld_leases", "Leases", source.collect, ("pool",))
    assert registry.render() == "# HELP rlworld_leases Leases\n# TYPE rlworld_leases gauge\n"
    source.samples = [(("linux",), 3.0), (("darwin",), 1.0)]
    assert registry.render().endswith('rlworld_leases{pool="darwin"} 1\nrlworld_leases{pool="linux"} 3\n')


@pytest.mark.parametrize("misuse", ["duplicate", "labels", "negative", "kind"])
def test_misuse_fails_loudly(misuse):
    registry = MetricsRegistry()
    counter = registry.counter("rlworld_a_total", "a", ("pool",))
    with pytest.raises(ValueError):
        if misuse == "duplicate":
            registry.gauge("rlworld_a_total", "again")
        elif misuse == "labels":
            counter.inc()
        elif misuse == "negative":
            counter.add(-1.0, "linux")
        else:
            counter.set(1.0, "linux")


def samples_of(registry):
    """Rendered sample lines keyed by name and labels."""
    samples = {}
    for line in registry.render().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.rpartition(" ")
            samples[key] = float(value)
    return samples


def call(purpose="policy", outcome="ok", cost=0.02, latency=1.5, waited=0.0, error=None, cached=False):
    return CallRecord(endpoint="local_policy", requested_model="m", served_model="m", purpose=purpose, run_id="run_1",
                      outcome=outcome, latency_seconds=latency, cost_usd=cost, rate_limit_wait_seconds=waited,
                      error=error, cached=cached)


def test_gateway_calls_count_latency_rate_limits_and_spend_by_purpose():
    metrics = PlatformMetrics()
    meter = UsageMeter(listeners=(metrics,))
    meter.record(call())
    meter.record(call(purpose="judge", cost=0.5, waited=2.0))
    meter.record(call(outcome="error", cost=None, error="RateLimitTimeoutError: waited too long"))
    meter.record(call(cached=True, cost=None))
    samples = samples_of(metrics.registry)
    assert samples['rlworld_gateway_calls_total{endpoint="local_policy",purpose="policy",outcome="ok"}'] == 2
    assert samples['rlworld_gateway_calls_total{endpoint="local_policy",purpose="policy",outcome="error"}'] == 1
    assert samples['rlworld_gateway_call_seconds_count{endpoint="local_policy",purpose="policy"}'] == 1
    assert samples['rlworld_gateway_call_seconds_sum{endpoint="local_policy",purpose="judge"}'] == 1.5
    assert samples['rlworld_gateway_rate_limit_hits_total{endpoint="local_policy"}'] == 2
    assert samples['rlworld_gateway_spend_usd_total{purpose="judge"}'] == 0.5
    assert samples['rlworld_gateway_spend_usd_total{purpose="policy"}'] == 0.02
    assert len(meter.records) == 4


def test_episodes_count_by_image_and_by_each_app_of_their_world():
    metrics = PlatformMetrics()
    observer = EpisodeMetrics(metrics, "noble_calc_v1", ("mail", "files"))
    for termination in ("success", "env_error", "env_error"):
        observer.episode_scored(termination)
    samples = samples_of(metrics.registry)
    assert samples['rlworld_episodes_total{image="noble_calc_v1",termination="env_error"}'] == 2
    assert samples['rlworld_app_episodes_total{app="mail",termination="success"}'] == 1
    assert samples['rlworld_app_episodes_total{app="files",termination="env_error"}'] == 2


def test_fidelity_calibration_and_divergence_keep_their_latest_values():
    metrics = PlatformMetrics()
    metrics.clone_fidelity_measured(clone_fidelity("crm", "a", "b", [(1.0, 0.0), (1.0, 1.0)]))
    metrics.verifier_calibrated(0.96, 0.9)
    metrics.verifier_calibrated(0.97, 0.92)
    metrics.replay_diverged(DivergenceReport(compared_steps=10, matching_steps=9, first_divergent_step=4))
    samples = samples_of(metrics.registry)
    assert samples['rlworld_clone_fidelity{app="crm"}'] == 0.5
    assert (samples["rlworld_verifier_precision"], samples["rlworld_verifier_recall"]) == (0.97, 0.92)
    assert samples['rlworld_replay_divergence_rate_bucket{le="0.1"}'] == 1
    assert samples['rlworld_replay_divergence_rate_bucket{le="0.05"}'] == 0


def broker():
    return EntitlementBroker(seat_pools=[SeatPoolConfig("office_ltsc", 4)],
                             identity_pools=[IdentityPoolConfig("finance_manager", ("fm1@corp.example",
                                                                                    "fm2@corp.example"))],
                             session_minter=SignedSessionMinter(b"signing key", "crm.example", time.time),
                             secret_issuer=EphemeralSecretIssuer(time.time))


def test_seats_and_identities_show_what_is_leased_of_each_pool():
    metrics = PlatformMetrics()
    assert "rlworld_seats_in_use{" not in metrics.registry.render()
    seats = broker()
    metrics.entitlements = seats
    seats.acquire(EntitlementRequest(episode_id="episode_1", run_id="run_1", tenant="lab", host_id="host_a",
                                     licenses=("office_ltsc",), personas=("finance_manager",), secrets=()))
    samples = samples_of(metrics.registry)
    assert (samples['rlworld_seats_in_use{product="office_ltsc"}'],
            samples['rlworld_seats_total{product="office_ltsc"}']) == (1, 4)
    assert (samples['rlworld_identities_in_use{pool="finance_manager"}'],
            samples['rlworld_identities_total{pool="finance_manager"}']) == (1, 2)


def test_runs_report_their_cost_per_thousand_scored_episodes(example_payload, tmp_path):
    service = build_experiment_service(eval_payload(example_payload), tmp_path)
    metrics = PlatformMetrics()
    metrics.runs = service
    digest = service.save_spec(dump_spec_document(parse_spec_document(eval_payload(example_payload))))
    experiment = service.launch(digest, budget_usd=100.0)
    run_id = experiment.run_ids[0]
    service.record_spend(run_id, 3.0)
    assert samples_of(metrics.registry) == {}
    service.publish_metrics(run_id, {"episodes": 1600, "env_errors": 80, "human_intervened": 20})
    service.record_spend(run_id, 1.5)
    assert samples_of(metrics.registry) == {f'rlworld_run_cost_per_1000_episodes_usd{{run_id="{run_id}"}}': 3.0}


def test_the_catalog_lists_exactly_what_experimentd_exposes():
    catalog = [metric for metric in json.loads(CATALOG.read_text())["metrics"] if metric["service"] == "experimentd"]
    exposed = PlatformMetrics().registry.described()
    assert {metric["name"]: (metric["type"], *metric["labels"]) for metric in catalog} == exposed
    helps = {family.name: family.help_text for family in PlatformMetrics().registry.families}
    assert {metric["name"]: metric["help"] for metric in catalog} == helps
