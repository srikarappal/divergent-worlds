"""Endpoint probes for preflight: reachability, latency, rate limits and token level output."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from rlworld.specs.models import EndpointSpec


@dataclass(frozen=True)
class ProbeResult:
    reachable: bool
    latency_seconds: float | None = None
    returns_token_ids: bool = False
    returns_logprobs: bool = False
    rate_limit_tokens_per_minute: int | None = None
    provider_kind: Literal["frontier", "self_hosted", "user"] = "user"
    error: str | None = None


class EndpointProbe(Protocol):
    def probe(self, endpoint: EndpointSpec) -> ProbeResult: ...


class StaticEndpointProbe:
    """Answers from a fixed table keyed by endpoint; used offline and until the gateway probes live."""

    def __init__(self, results: Mapping[str, ProbeResult]) -> None:
        self.results = dict(results)

    def probe(self, endpoint: EndpointSpec) -> ProbeResult:
        return self.results.get(
            endpoint.endpoint, ProbeResult(reachable=False, error=f"no probe result for {endpoint.endpoint}")
        )
