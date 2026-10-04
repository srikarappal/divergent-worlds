"""Prometheus metrics experimentd exposes on /metrics (DESIGN.md section 6.15).

A small registry of counters, gauges and histograms written in the Prometheus
text format; gauges computed at scrape time come from collector functions.
PlatformMetrics names the platform health signals experimentd owns: episodes
by image, app and termination; gateway calls, latency, rate limit hits and
spend; seat and identity use; verifier calibration; replay divergence; clone
fidelity and each run's cost per 1,000 scored episodes. schemas/metrics.json
lists every one and tests hold this registry to it.
"""

import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from rlworld.analysis.fidelity import CloneFidelity
from rlworld.gateway.metering import CallRecord
from rlworld.trajectories.replay import DivergenceReport

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
DIVERGENCE_BUCKETS = (0.0, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0)
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
RATE_LIMIT_ERROR_MARKER = "RateLimit"

MetricKind = Literal["counter", "gauge", "histogram"]
Sample = tuple[tuple[str, ...], float]
Collector = Callable[[], list[Sample]]


@dataclass
class Series:
    value: float = 0.0
    bucket_counts: list[int] = field(default_factory=list)
    count: int = 0


class MetricFamily:
    def __init__(self, lock: threading.Lock, name: str, help_text: str, kind: MetricKind,
                 label_names: Sequence[str], buckets: Sequence[float] = (), collect: Collector | None = None) -> None:
        self.lock = lock
        self.name = name
        self.help_text = help_text
        self.kind = kind
        self.label_names = tuple(label_names)
        self.buckets = tuple(sorted(buckets))
        self.collect = collect
        self.series: dict[tuple[str, ...], Series] = {}

    def series_for(self, label_values: Sequence[str]) -> Series:
        """The caller holds the registry lock."""
        if len(label_values) != len(self.label_names):
            raise ValueError(f"{self.name} takes {len(self.label_names)} label values, got {len(label_values)}")
        key = tuple(label_values)
        if key not in self.series:
            self.series[key] = Series(bucket_counts=[0] * len(self.buckets))
        return self.series[key]

    def add(self, amount: float, *label_values: str) -> None:
        if self.kind != "counter" or amount < 0:
            raise ValueError(f"{self.name} only counts up")
        with self.lock:
            self.series_for(label_values).value += amount

    def inc(self, *label_values: str) -> None:
        self.add(1.0, *label_values)

    def set(self, value: float, *label_values: str) -> None:
        if self.kind != "gauge":
            raise ValueError(f"{self.name} is not a gauge")
        with self.lock:
            self.series_for(label_values).value = value

    def observe(self, value: float, *label_values: str) -> None:
        if self.kind != "histogram":
            raise ValueError(f"{self.name} is not a histogram")
        with self.lock:
            series = self.series_for(label_values)
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    series.bucket_counts[index] += 1
            series.count += 1
            series.value += value


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def label_text(names: Sequence[str], values: Sequence[str], extra: tuple[str, str] | None = None) -> str:
    pairs = [f'{name}="{escape_label_value(value)}"' for name, value in zip(names, values)]
    if extra is not None:
        pairs.append(f'{extra[0]}="{extra[1]}"')
    return "{" + ",".join(pairs) + "}" if pairs else ""


def family_lines(family: MetricFamily, series: list[tuple[tuple[str, ...], Series]]) -> list[str]:
    help_text = family.help_text.replace("\\", "\\\\").replace("\n", "\\n")
    lines = [f"# HELP {family.name} {help_text}", f"# TYPE {family.name} {family.kind}"]
    for label_values, values in series:
        labels = label_text(family.label_names, label_values)
        if family.kind != "histogram":
            lines.append(f"{family.name}{labels} {format_value(values.value)}")
            continue
        for bound, bucket_count in zip(family.buckets, values.bucket_counts):
            bucket_labels = label_text(family.label_names, label_values, ("le", format_value(bound)))
            lines.append(f"{family.name}_bucket{bucket_labels} {bucket_count}")
        everything = label_text(family.label_names, label_values, ("le", "+Inf"))
        lines.append(f"{family.name}_bucket{everything} {values.count}")
        lines.append(f"{family.name}_sum{labels} {format_value(values.value)}")
        lines.append(f"{family.name}_count{labels} {values.count}")
    return lines


class MetricsRegistry:
    """Families are written in registration order and series in label order, so scrapes read the same."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.families: list[MetricFamily] = []

    def register(self, family: MetricFamily) -> MetricFamily:
        if any(existing.name == family.name for existing in self.families):
            raise ValueError(f"metric {family.name} is registered twice")
        self.families.append(family)
        return family

    def counter(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> MetricFamily:
        return self.register(MetricFamily(self.lock, name, help_text, "counter", label_names))

    def gauge(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> MetricFamily:
        return self.register(MetricFamily(self.lock, name, help_text, "gauge", label_names))

    def gauge_function(self, name: str, help_text: str, collect: Collector,
                       label_names: Sequence[str] = ()) -> MetricFamily:
        return self.register(MetricFamily(self.lock, name, help_text, "gauge", label_names, collect=collect))

    def histogram(self, name: str, help_text: str, buckets: Sequence[float],
                  label_names: Sequence[str] = ()) -> MetricFamily:
        return self.register(MetricFamily(self.lock, name, help_text, "histogram", label_names, buckets))

    def described(self) -> dict[str, tuple[str, ...]]:
        """Each family's kind and label names, for parity checks against schemas/metrics.json."""
        return {family.name: (family.kind, *family.label_names) for family in self.families}

    def render(self) -> str:
        lines = []
        for family in self.families:
            if family.collect is not None:
                series = [(tuple(labels), Series(value=value)) for labels, value in sorted(family.collect())]
            else:
                with self.lock:
                    series = sorted((labels, Series(values.value, list(values.bucket_counts), values.count))
                                    for labels, values in family.series.items())
            lines.extend(family_lines(family, series))
        return "\n".join(lines) + "\n"


class SeatSource(Protocol):
    seat_pools: dict[str, Any]
    identity_pool_sizes: dict[str, int]

    def availability(self) -> dict[str, int]: ...


class RunCostSource(Protocol):
    def cost_per_thousand_episodes(self) -> dict[str, float]: ...


class PlatformMetrics:
    """experimentd's platform health signals; entitlements and runs are set once those services exist."""

    def __init__(self, registry: MetricsRegistry | None = None) -> None:
        self.registry = registry or MetricsRegistry()
        self.entitlements: SeatSource | None = None
        self.runs: RunCostSource | None = None
        add_counter, add_gauge, add_histogram = self.registry.counter, self.registry.gauge, self.registry.histogram
        self.episodes = add_counter("rlworld_episodes_total", "Scored episodes by image and termination",
                                    ("image", "termination"))
        self.app_episodes = add_counter("rlworld_app_episodes_total",
                                        "Scored episodes by app of their world and termination", ("app", "termination"))
        self.gateway_calls = add_counter("rlworld_gateway_calls_total",
                                         "Model gateway calls by endpoint, purpose and outcome",
                                         ("endpoint", "purpose", "outcome"))
        self.gateway_seconds = add_histogram(
            "rlworld_gateway_call_seconds",
            "Latency of answered gateway calls that missed the cache; purpose policy is the model part of a step",
            LATENCY_BUCKETS, ("endpoint", "purpose"))
        self.rate_limit_hits = add_counter("rlworld_gateway_rate_limit_hits_total",
                                           "Gateway calls that waited on or were refused by an endpoint rate limit",
                                           ("endpoint",))
        self.spend = add_counter("rlworld_gateway_spend_usd_total",
                                 "Model spend in USD by purpose; purpose judge is judge spend", ("purpose",))
        self.registry.gauge_function("rlworld_seats_in_use", "License seats leased right now, by product",
                                     self.seats_in_use, ("product",))
        self.registry.gauge_function("rlworld_seats_total", "License seats in each pool", self.seats_total,
                                     ("product",))
        self.registry.gauge_function("rlworld_identities_in_use", "Identities leased right now, by identity pool",
                                     self.identities_in_use, ("pool",))
        self.registry.gauge_function("rlworld_identities_total", "Identities in each identity pool",
                                     self.identities_total, ("pool",))
        self.verifier_precision = add_gauge("rlworld_verifier_precision",
                                            "Verifier precision against human labels at the last calibration")
        self.verifier_recall = add_gauge("rlworld_verifier_recall",
                                         "Verifier recall against human labels at the last calibration")
        self.replay_divergence = add_histogram(
            "rlworld_replay_divergence_rate",
            "Share of steps whose observation diverged when actions were rerun from the start snapshot",
            DIVERGENCE_BUCKETS)
        self.fidelity = add_gauge("rlworld_clone_fidelity",
                                  "One minus the success gap between an app clone and the real app at the last "
                                  "measurement", ("app",))
        self.registry.gauge_function("rlworld_run_cost_per_1000_episodes_usd",
                                     "A run's spend per 1,000 scored episodes", self.run_costs, ("run_id",))

    def call_recorded(self, record: CallRecord) -> None:
        """UsageMeter listener: one gateway call."""
        self.gateway_calls.inc(record.endpoint, record.purpose, record.outcome)
        if record.outcome == "ok" and not record.cached:
            self.gateway_seconds.observe(record.latency_seconds, record.endpoint, record.purpose)
        if record.rate_limit_wait_seconds > 0 or RATE_LIMIT_ERROR_MARKER in (record.error or ""):
            self.rate_limit_hits.inc(record.endpoint)
        if record.cost_usd:
            self.spend.add(record.cost_usd, record.purpose)

    def episode_finished(self, image: str, apps: Sequence[str], termination: str) -> None:
        self.episodes.inc(image, termination)
        for app in apps:
            self.app_episodes.inc(app, termination)

    def clone_fidelity_measured(self, report: CloneFidelity) -> None:
        self.fidelity.set(report.fidelity, report.app)

    def verifier_calibrated(self, precision: float, recall: float) -> None:
        self.verifier_precision.set(precision)
        self.verifier_recall.set(recall)

    def replay_diverged(self, report: DivergenceReport) -> None:
        self.replay_divergence.observe(report.divergence_rate)

    def seats_in_use(self) -> list[Sample]:
        if self.entitlements is None:
            return []
        free = self.entitlements.availability()
        return [((name,), float(state.config.capacity - free[name]))
                for name, state in self.entitlements.seat_pools.items()]

    def seats_total(self) -> list[Sample]:
        if self.entitlements is None:
            return []
        return [((name,), float(state.config.capacity)) for name, state in self.entitlements.seat_pools.items()]

    def identities_in_use(self) -> list[Sample]:
        if self.entitlements is None:
            return []
        free = self.entitlements.availability()
        return [((pool,), float(total - free[f"identity:{pool}"])) for (pool,), total in self.identity_sizes()]

    def identities_total(self) -> list[Sample]:
        return [((pool,), float(total)) for (pool,), total in self.identity_sizes()]

    def identity_sizes(self) -> list[Sample]:
        if self.entitlements is None:
            return []
        return [((pool,), float(total)) for pool, total in self.entitlements.identity_pool_sizes.items()]

    def run_costs(self) -> list[Sample]:
        if self.runs is None:
            return []
        return [((run_id,), cost) for run_id, cost in self.runs.cost_per_thousand_episodes().items()]


class EpisodeMetrics:
    """A reward service observer that counts one run's scored episodes by its image and apps."""

    def __init__(self, metrics: PlatformMetrics, image: str, apps: Sequence[str]) -> None:
        self.metrics = metrics
        self.image = image
        self.apps = tuple(apps)

    def episode_scored(self, termination: str) -> None:
        self.metrics.episode_finished(self.image, self.apps, termination)
