"""Per call metering: model version, sampling, latency, tokens, cost and errors (DESIGN.md 6.8)."""

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class CallRecord:
    endpoint: str
    requested_model: str
    served_model: str
    purpose: str
    run_id: str
    outcome: Literal["ok", "error"]
    latency_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    attempts: int = 0
    cached: bool = False
    rate_limit_wait_seconds: float = 0.0
    error: str | None = None
    sampling: dict[str, Any] = field(default_factory=dict)

    @property
    def model_mismatch(self) -> bool:
        """The provider served something other than the pinned model, such as a moved alias."""
        return self.outcome == "ok" and not self.cached and self.served_model != self.requested_model


@dataclass
class UsageSummary:
    calls: int = 0
    errors: int = 0
    cache_hits: int = 0
    model_mismatches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0
    total_latency_seconds: float = 0.0
    rate_limit_wait_seconds: float = 0.0

    @property
    def mean_latency_seconds(self) -> float:
        successful_live_calls = self.calls - self.errors - self.cache_hits
        return self.total_latency_seconds / successful_live_calls if successful_live_calls else 0.0


class UsageMeter:
    def __init__(self) -> None:
        self.records: list[CallRecord] = []
        self.lock = threading.Lock()

    def record(self, call_record: CallRecord) -> None:
        with self.lock:
            self.records.append(call_record)

    def summary(self, run_id: str | None = None) -> dict[str, UsageSummary]:
        """Usage per endpoint, optionally for one run."""
        with self.lock:
            selected_records = [record for record in self.records if run_id is None or record.run_id == run_id]
        summaries: dict[str, UsageSummary] = defaultdict(UsageSummary)
        for record in selected_records:
            summary = summaries[record.endpoint]
            summary.calls += 1
            summary.errors += record.outcome == "error"
            summary.cache_hits += record.cached
            summary.model_mismatches += record.model_mismatch
            summary.input_tokens += record.input_tokens
            summary.output_tokens += record.output_tokens
            summary.rate_limit_wait_seconds += record.rate_limit_wait_seconds
            if record.outcome == "ok" and not record.cached:
                summary.total_latency_seconds += record.latency_seconds
                if record.cost_usd is None:
                    summary.unpriced_calls += 1
                else:
                    summary.cost_usd += record.cost_usd
        return dict(summaries)
