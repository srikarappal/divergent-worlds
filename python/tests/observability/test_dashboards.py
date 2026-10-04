import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OBSERVABILITY = REPO_ROOT / "observability"
DASHBOARD = OBSERVABILITY / "grafana" / "dashboards" / "platform_health.json"
CATALOG = {metric["name"]: metric for metric in json.loads((REPO_ROOT / "schemas" / "metrics.json").read_text())[
    "metrics"]}
METRIC_PATTERN = re.compile(r"\brlworld_[a-z0-9_]+")
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
# Each health signal DESIGN.md 6.15 names, with a word its panel title carries.
DESIGN_SIGNALS = {
    "lease latency p50/p99 per pool": "Lease latency",
    "restore failures": "Restore failures",
    "env error rate per image": "Env error rate per image",
    "env error rate per app": "Env error rate per app",
    "step latency split": "Step latency split",
    "seat and identity utilization": "Seat and identity utilization",
    "gateway rate limit hits": "Gateway rate limit hits",
    "judge spend": "Judge spend",
    "verifier precision and recall": "Verifier precision and recall",
    "replay divergence": "Replay divergence",
    "clone fidelity": "Clone fidelity",
    "cost per 1,000 episodes": "Cost per 1,000 episodes",
}


@pytest.fixture(scope="module")
def dashboard():
    return json.loads(DASHBOARD.read_text())


def expressions(dashboard):
    return [(panel["title"], target["expr"]) for panel in dashboard["panels"] for target in panel["targets"]]


def catalog_name(reference):
    """A histogram is queried through its bucket, sum and count series."""
    for suffix in HISTOGRAM_SUFFIXES:
        base = reference.removesuffix(suffix)
        if base != reference and CATALOG.get(base, {}).get("type") == "histogram":
            return base
    return reference


def test_every_panel_queries_the_provisioned_prometheus(dashboard):
    datasource = yaml.safe_load((OBSERVABILITY / "grafana" / "provisioning" / "datasources" / "prometheus.yaml")
                                .read_text())["datasources"][0]
    assert (datasource["type"], datasource["uid"]) == ("prometheus", "rlworld-prometheus")
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids)) and dashboard["uid"] == "rlworld-platform-health"
    for panel in dashboard["panels"]:
        assert panel["datasource"] == {"type": "prometheus", "uid": datasource["uid"]}
        assert panel["targets"] and set(panel["gridPos"]) == {"h", "w", "x", "y"}
        assert all(target["datasource"] == panel["datasource"] for target in panel["targets"])
        assert len({target["refId"] for target in panel["targets"]}) == len(panel["targets"])


def test_panels_query_only_cataloged_metrics_and_chart_every_one(dashboard):
    referenced = {catalog_name(reference) for _, expr in expressions(dashboard)
                  for reference in METRIC_PATTERN.findall(expr)}
    assert referenced == set(CATALOG)


def test_counters_are_rated_and_histograms_are_read_through_their_buckets(dashboard):
    for title, expr in expressions(dashboard):
        for reference in METRIC_PATTERN.findall(expr):
            metric = CATALOG[catalog_name(reference)]
            if metric["type"] == "counter":
                assert re.search(rf"(rate|increase)\({reference}\b", expr), f"{title}: {reference} is not rated"
            if metric["type"] == "histogram":
                assert reference.endswith("_bucket") and "histogram_quantile(" in expr, title


def test_expressions_balance_their_brackets(dashboard):
    pairs = {")": "(", "]": "[", "}": "{"}
    for title, expr in expressions(dashboard):
        stack = []
        for character in re.sub(r'"[^"]*"', "", expr):
            if character in "([{":
                stack.append(character)
            elif character in pairs:
                assert stack and stack.pop() == pairs[character], f"{title}: {expr}"
        assert not stack, f"{title}: {expr}"


def test_labels_used_in_queries_exist_on_their_metrics(dashboard):
    for title, expr in expressions(dashboard):
        for reference, selector in re.findall(r"\b(rlworld_[a-z0-9_]+)\{([^}]*)\}", expr):
            metric = CATALOG[catalog_name(reference)]
            allowed = {*metric["labels"], *(("le",) if metric["type"] == "histogram" else ())}
            assert {pair.split("=")[0].strip() for pair in selector.split(",")} <= allowed, title
        for grouping in re.findall(r"sum by \(([^)]*)\)", expr):
            labels = {label.strip() for label in grouping.split(",")}
            names = [catalog_name(reference) for reference in METRIC_PATTERN.findall(expr)]
            available = set().union(*(CATALOG[name]["labels"] for name in names)) | {"le"}
            assert labels <= available, f"{title}: grouping by {labels}"


def test_every_health_signal_the_design_names_has_a_panel(dashboard):
    titles = [panel["title"] for panel in dashboard["panels"]]
    for signal, title_start in DESIGN_SIGNALS.items():
        assert any(title.startswith(title_start) for title in titles), signal


def test_prometheus_scrapes_both_services_and_grafana_loads_the_dashboards():
    scrape = yaml.safe_load((OBSERVABILITY / "prometheus" / "prometheus.yml").read_text())
    jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
    assert set(jobs) == {metric["service"] for metric in CATALOG.values()}
    assert all(job["metrics_path"] == "/metrics" for job in jobs.values())
    provider = yaml.safe_load((OBSERVABILITY / "grafana" / "provisioning" / "dashboards" / "rlworld.yaml")
                              .read_text())["providers"][0]
    assert provider["type"] == "file" and provider["options"]["path"].endswith("/rlworld")
