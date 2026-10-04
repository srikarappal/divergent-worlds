"""Gateway clients for the judge, the failure labeler and endpoint probes (DESIGN.md 6.8, 6.9 and 6.14).

Every model call leaves through the gateway, so judge and labeler requests are
metered, cached and rate limited like policy calls. Testing an endpoint is one
tiny request along the path its runs will take.
"""

from typing import Protocol

from rlworld.gateway.config import EndpointRegistry
from rlworld.gateway.errors import GatewayError, TokenIdsUnavailableError
from rlworld.gateway.messages import ChatRequest, ChatResponse
from rlworld.judge.prompt import JudgeRequest
from rlworld.preflight.probes import ProbeResult
from rlworld.specs.models import EndpointSpec

PROBE_PROMPT = "Reply with the single word ok."


class ChatGateway(Protocol):
    def complete(self, request: ChatRequest) -> ChatResponse: ...


class GatewayJudgeClient:
    """Sends judge and failure labeler requests to one endpoint through the gateway."""

    def __init__(self, gateway: ChatGateway, endpoint: str, run_id: str = "", max_tokens: int = 2048) -> None:
        self.gateway = gateway
        self.endpoint = endpoint
        self.run_id = run_id
        self.max_tokens = max_tokens

    def complete(self, request: JudgeRequest) -> str:
        return self.gateway.complete(ChatRequest(
            endpoint=self.endpoint, model=request.model, messages=request.messages, temperature=request.temperature,
            max_tokens=self.max_tokens, response_schema=request.response_schema, sample_index=request.sample_index,
            run_id=self.run_id, purpose="judge")).text


class GatewayEndpointProbe:
    """Tests an endpoint with a one token request; asks for token ids when the run needs them."""

    def __init__(self, gateway: ChatGateway, registry: EndpointRegistry) -> None:
        self.gateway = gateway
        self.registry = registry

    def probe(self, endpoint: EndpointSpec) -> ProbeResult:
        try:
            config = self.registry.resolve(endpoint.endpoint)
        except GatewayError as error:
            return ProbeResult(reachable=False, error=str(error))
        request = ChatRequest(endpoint=config.name, model=endpoint.model,
                              messages=({"role": "user", "content": PROBE_PROMPT},), max_tokens=1,
                              require_token_ids=endpoint.token_ids or endpoint.logprobs, purpose="probe")
        try:
            response = self.gateway.complete(request)
        except TokenIdsUnavailableError as error:
            return ProbeResult(reachable=True, rate_limit_tokens_per_minute=config.tokens_per_minute,
                               provider_kind=config.provider_kind, error=str(error))
        except GatewayError as error:
            return ProbeResult(reachable=False, provider_kind=config.provider_kind, error=str(error))
        trace = response.token_trace
        return ProbeResult(reachable=True, latency_seconds=response.latency_seconds,
                           returns_token_ids=trace is not None,
                           returns_logprobs=trace is not None and bool(trace.completion_logprobs),
                           rate_limit_tokens_per_minute=config.tokens_per_minute,
                           provider_kind=config.provider_kind)
