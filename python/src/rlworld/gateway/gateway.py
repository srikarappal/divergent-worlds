"""The model gateway: one router for policy and judge calls (DESIGN.md section 6.8).

Order of a call: resolve the endpoint, enforce the pinned model and the data
policy, consult the eval cache, check the circuit, wait for rate limit and
concurrency headroom, then send with jittered retries. Every call, failed or
not, is metered.
"""

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from rlworld.gateway.cache import ResponseCache
from rlworld.gateway.config import EndpointConfig, EndpointRegistry
from rlworld.gateway.errors import (
    CacheMissError,
    DataPolicyViolationError,
    GatewayError,
    ModelNotPinnedError,
    RateLimitTimeoutError,
    TokenIdsUnavailableError,
    UpstreamError,
)
from rlworld.gateway.messages import (
    ChatRequest,
    ChatResponse,
    Usage,
    contains_images,
    estimate_request_tokens,
    request_digest,
)
from rlworld.gateway.metering import CallRecord, UsageMeter
from rlworld.gateway.resilience import CircuitBreaker, CircuitState, TokenBucket, retry_delay
from rlworld.gateway.secrets import SecretStore


class Transport(Protocol):
    def send(self, endpoint: EndpointConfig, api_key: str | None, request: ChatRequest) -> dict[str, Any]: ...


class ScreenshotPolicy(Protocol):
    """Per tenant data policy: may this run's frames go to this endpoint (DESIGN.md 6.7, 6.14)."""

    def allows_screenshots(self, run_id: str, endpoint_name: str) -> bool: ...


@dataclass
class EndpointState:
    concurrency: threading.BoundedSemaphore
    circuit: CircuitBreaker
    request_bucket: TokenBucket | None
    token_bucket: TokenBucket | None


@dataclass(frozen=True)
class EndpointHeadroom:
    """What admission control reads so envs are not started behind a rate limit."""

    requests_available: float | None
    tokens_available: float | None
    circuit_state: CircuitState


def call_cost(endpoint: EndpointConfig, usage: Usage) -> float | None:
    if endpoint.input_price_per_million_usd is None or endpoint.output_price_per_million_usd is None:
        return None
    cached_price = endpoint.cached_input_price_per_million_usd
    if cached_price is None:
        cached_price = endpoint.input_price_per_million_usd
    uncached_input_tokens = usage.input_tokens - usage.cached_input_tokens
    return (
        uncached_input_tokens * endpoint.input_price_per_million_usd
        + usage.cached_input_tokens * cached_price
        + usage.output_tokens * endpoint.output_price_per_million_usd
    ) / 1_000_000


class ModelGateway:
    def __init__(
        self,
        registry: EndpointRegistry,
        transport: Transport,
        secret_store: SecretStore,
        response_cache: ResponseCache | None = None,
        meter: UsageMeter | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_generator: random.Random | None = None,
        retry_base_seconds: float = 0.5,
        retry_max_seconds: float = 20.0,
        wait_timeout_seconds: float = 60.0,
        circuit_failure_threshold: int = 5,
        circuit_cooldown_seconds: float = 30.0,
        screenshot_policy: ScreenshotPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.transport = transport
        self.secret_store = secret_store
        self.response_cache = response_cache
        self.meter = meter or UsageMeter()
        self.clock = clock
        self.sleep = sleep
        self.random_generator = random_generator or random.Random()
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.wait_timeout_seconds = wait_timeout_seconds
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self.screenshot_policy = screenshot_policy
        self.endpoint_states: dict[str, EndpointState] = {}
        self.states_lock = threading.Lock()

    def endpoint_state(self, endpoint: EndpointConfig) -> EndpointState:
        with self.states_lock:
            state = self.endpoint_states.get(endpoint.name)
            if state is None:
                state = EndpointState(
                    concurrency=threading.BoundedSemaphore(endpoint.max_concurrency),
                    circuit=CircuitBreaker(self.circuit_failure_threshold, self.circuit_cooldown_seconds, self.clock),
                    request_bucket=(
                        TokenBucket(endpoint.requests_per_minute, self.clock, self.sleep)
                        if endpoint.requests_per_minute
                        else None
                    ),
                    token_bucket=(
                        TokenBucket(endpoint.tokens_per_minute, self.clock, self.sleep)
                        if endpoint.tokens_per_minute
                        else None
                    ),
                )
                self.endpoint_states[endpoint.name] = state
            return state

    def headroom(self, endpoint_reference: str) -> EndpointHeadroom:
        state = self.endpoint_state(self.registry.resolve(endpoint_reference))
        return EndpointHeadroom(
            requests_available=state.request_bucket.available() if state.request_bucket else None,
            tokens_available=state.token_bucket.available() if state.token_bucket else None,
            circuit_state=state.circuit.state,
        )

    def complete(self, request: ChatRequest) -> ChatResponse:
        endpoint = self.registry.resolve(request.endpoint)
        if request.model not in endpoint.allowed_models:
            raise ModelNotPinnedError(f"{request.model} is not a pinned model on {endpoint.name}")
        if contains_images(request) and not self.screenshots_allowed(request.run_id, endpoint):
            raise DataPolicyViolationError(f"the tenant data policy forbids sending screenshots to {endpoint.name}")
        cache_key = request_digest(endpoint.name, request) if request.cache_mode != "off" else ""
        if cache_key and self.response_cache is not None:
            cached_response = self.response_cache.get(cache_key)
            if cached_response is not None:
                self.meter_call(endpoint, request, "ok", response=cached_response, cached=True)
                return cached_response.model_copy(update={"cached": True, "latency_seconds": 0.0})
        if request.cache_mode == "read_only":
            raise CacheMissError(f"no recorded response for this request on {endpoint.name}")
        response = self.send_with_retries(endpoint, request)
        if cache_key and self.response_cache is not None:
            self.response_cache.put(cache_key, response)
        return response

    def screenshots_allowed(self, run_id: str, endpoint: EndpointConfig) -> bool:
        if not endpoint.accepts_screenshots:
            return False
        return self.screenshot_policy is None or self.screenshot_policy.allows_screenshots(run_id, endpoint.name)

    def send_with_retries(self, endpoint: EndpointConfig, request: ChatRequest) -> ChatResponse:
        state = self.endpoint_state(endpoint)
        api_key = self.secret_store.get(endpoint.api_key_secret) if endpoint.api_key_secret else None
        state.circuit.before_call()
        estimated_tokens = estimate_request_tokens(request)
        try:
            waited_seconds = self.wait_for_headroom(state, estimated_tokens)
        except RateLimitTimeoutError as error:
            state.circuit.release_trial()
            self.meter_call(endpoint, request, "error", error=str(error))
            raise
        if not state.concurrency.acquire(timeout=self.wait_timeout_seconds):
            state.circuit.release_trial()
            error = RateLimitTimeoutError(f"{endpoint.name} stayed at its concurrency limit")
            self.meter_call(endpoint, request, "error", error=str(error), waited_seconds=waited_seconds)
            raise error
        try:
            return self.attempt_calls(endpoint, request, state, api_key, estimated_tokens, waited_seconds)
        finally:
            state.concurrency.release()

    def wait_for_headroom(self, state: EndpointState, estimated_tokens: int) -> float:
        waited_seconds = 0.0
        if state.request_bucket is not None:
            waited_seconds += state.request_bucket.acquire(1, self.wait_timeout_seconds)
        if state.token_bucket is not None:
            waited_seconds += state.token_bucket.acquire(estimated_tokens, self.wait_timeout_seconds)
        return waited_seconds

    def attempt_calls(
        self,
        endpoint: EndpointConfig,
        request: ChatRequest,
        state: EndpointState,
        api_key: str | None,
        estimated_tokens: int,
        waited_seconds: float,
    ) -> ChatResponse:
        for attempt_index in range(endpoint.max_attempts):
            started_at = self.clock()
            try:
                parsed_fields = self.transport.send(endpoint, api_key, request)
            except UpstreamError as error:
                if error.counts_as_endpoint_failure:
                    state.circuit.record_failure()
                else:
                    state.circuit.release_trial()
                is_last_attempt = attempt_index + 1 == endpoint.max_attempts
                if not error.retryable or is_last_attempt:
                    self.meter_call(endpoint, request, "error", error=str(error), attempts=attempt_index + 1,
                                    waited_seconds=waited_seconds)
                    raise
                self.sleep(retry_delay(attempt_index, error.retry_after_seconds, self.retry_base_seconds,
                                       self.retry_max_seconds, self.random_generator))
                continue
            state.circuit.record_success()
            usage = parsed_fields["usage"]
            if state.token_bucket is not None:
                state.token_bucket.adjust(usage.input_tokens + usage.output_tokens - estimated_tokens)
            response = ChatResponse(
                **parsed_fields,
                latency_seconds=self.clock() - started_at,
                cost_usd=call_cost(endpoint, usage),
                attempts=attempt_index + 1,
            )
            if request.require_token_ids and response.token_trace is None:
                error = TokenIdsUnavailableError(f"{endpoint.name} returned no token ids for a training request")
                self.meter_call(endpoint, request, "error", response=response, error=str(error),
                                attempts=attempt_index + 1, waited_seconds=waited_seconds)
                raise error
            self.meter_call(endpoint, request, "ok", response=response, attempts=attempt_index + 1,
                            waited_seconds=waited_seconds)
            return response
        raise GatewayError("retry loop ended without a result")

    def meter_call(
        self,
        endpoint: EndpointConfig,
        request: ChatRequest,
        outcome: str,
        response: ChatResponse | None = None,
        error: str | None = None,
        attempts: int = 0,
        cached: bool = False,
        waited_seconds: float = 0.0,
    ) -> None:
        self.meter.record(
            CallRecord(
                endpoint=endpoint.name,
                requested_model=request.model,
                served_model=response.served_model if response else request.model,
                purpose=request.purpose,
                run_id=request.run_id,
                outcome=outcome,
                latency_seconds=response.latency_seconds if response and not cached else 0.0,
                input_tokens=response.usage.input_tokens if response and not cached else 0,
                output_tokens=response.usage.output_tokens if response and not cached else 0,
                cost_usd=response.cost_usd if response and not cached else None,
                attempts=attempts,
                cached=cached,
                rate_limit_wait_seconds=waited_seconds,
                error=error,
                sampling={
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                    "seed": request.seed,
                    "sample_index": request.sample_index,
                },
            )
        )
