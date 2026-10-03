"""Gateway adapters for the judge client and the preflight endpoint probe."""

import time
from collections.abc import Callable

from rlworld.gateway.config import EndpointRegistry
from rlworld.gateway.errors import GatewayError
from rlworld.gateway.gateway import ModelGateway
from rlworld.gateway.messages import ChatRequest
from rlworld.judge.prompt import JudgeRequest
from rlworld.preflight.probes import ProbeResult
from rlworld.specs.models import EndpointSpec

PROBE_PROMPT = "Reply with the single word OK."


class GatewayJudgeClient:
    """Sends judge requests through the gateway; the judge keeps its own verdict cache."""

    def __init__(self, gateway: ModelGateway, endpoint: str, run_id: str = "") -> None:
        self.gateway = gateway
        self.endpoint = endpoint
        self.run_id = run_id

    def complete(self, request: JudgeRequest) -> str:
        chat_request = ChatRequest(
            endpoint=self.endpoint,
            model=request.model,
            messages=request.messages,
            response_schema=request.response_schema,
            temperature=request.temperature,
            seed=request.sample_index,
            sample_index=request.sample_index,
            run_id=self.run_id,
            purpose="judge",
        )
        return self.gateway.complete(chat_request).text


class GatewayEndpointProbe:
    """Preflight probe: one tiny call per endpoint, asking for token ids when the endpoint claims them."""

    def __init__(
        self, gateway: ModelGateway, registry: EndpointRegistry, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.gateway = gateway
        self.registry = registry
        self.clock = clock

    def probe(self, endpoint: EndpointSpec) -> ProbeResult:
        try:
            endpoint_config = self.registry.resolve(endpoint.endpoint)
        except GatewayError as error:
            return ProbeResult(reachable=False, error=str(error))
        probe_request = ChatRequest(
            endpoint=endpoint_config.name,
            model=endpoint.model,
            messages=({"role": "user", "content": PROBE_PROMPT},),
            max_tokens=2,
            require_token_ids=endpoint_config.returns_token_ids,
            purpose="probe",
        )
        started_at = self.clock()
        try:
            response = self.gateway.complete(probe_request)
        except GatewayError as error:
            return ProbeResult(reachable=False, provider_kind=endpoint_config.provider_kind, error=str(error))
        has_token_trace = response.token_trace is not None
        return ProbeResult(
            reachable=True,
            latency_seconds=self.clock() - started_at,
            returns_token_ids=has_token_trace,
            returns_logprobs=has_token_trace and bool(response.token_trace.completion_logprobs),
            rate_limit_tokens_per_minute=endpoint_config.tokens_per_minute,
            provider_kind=endpoint_config.provider_kind,
        )
