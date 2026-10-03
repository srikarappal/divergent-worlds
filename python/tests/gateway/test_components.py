import random

import pytest
from support.gateway_fakes import FakeClock, chat_request

from rlworld.gateway import (
    EndpointConfig,
    EndpointNotFoundError,
    EndpointRegistry,
    EnvironmentSecretStore,
    MissingSecretError,
    load_endpoint_registry,
)
from rlworld.gateway.errors import CircuitOpenError, RateLimitTimeoutError
from rlworld.gateway.messages import contains_images, estimate_request_tokens, request_digest
from rlworld.gateway.openai_compatible import PassthroughImageResolver, build_payload, parse_response
from rlworld.gateway.resilience import CircuitBreaker, TokenBucket, retry_delay

IMAGE_MESSAGE = {
    "role": "user",
    "content": [{"type": "text", "text": "What is on screen?"},
                {"type": "image", "uri": "https://f.example.com/1.webp"}],
}


def test_request_digests_cover_what_shapes_the_answer():
    base_request = chat_request()
    assert request_digest("policy_vllm", base_request) == request_digest("policy_vllm", chat_request(run_id="run_2"))
    assert request_digest("policy_vllm", base_request) != request_digest("policy_vllm", chat_request(sample_index=1))
    assert request_digest("policy_vllm", base_request) != request_digest("other_endpoint", base_request)


def test_image_detection_and_token_estimates():
    image_request = chat_request(messages=(IMAGE_MESSAGE,), max_tokens=10)
    assert contains_images(image_request)
    assert not contains_images(chat_request())
    assert estimate_request_tokens(image_request) == len("What is on screen?") // 4 + 1500 + 10


def test_payload_conversion():
    request = chat_request(
        messages=(IMAGE_MESSAGE,),
        tools=({"name": "done", "description": "finish", "parameters": {"type": "object"}},),
        response_schema={"type": "object"},
        require_token_ids=True,
        seed=7,
    )
    payload = build_payload(request, PassthroughImageResolver())
    assert payload["messages"][0]["content"][1] == {"type": "image_url",
                                                    "image_url": {"url": "https://f.example.com/1.webp"}}
    assert payload["tools"] == [{"type": "function", "function": request.tools[0]}]
    assert payload["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert (payload["logprobs"], payload["return_token_ids"], payload["seed"]) == (True, True, 7)
    plain_payload = build_payload(chat_request(), PassthroughImageResolver())
    assert "logprobs" not in plain_payload and "tools" not in plain_payload


def test_unresolvable_image_uris_are_refused():
    with pytest.raises(ValueError):
        PassthroughImageResolver().resolve("s3://frames/1.webp")


def test_parse_response_without_token_ids():
    parsed = parse_response({"choices": [{"message": {"content": "hi"}}]}, "requested_model")
    assert parsed["token_trace"] is None
    assert parsed["served_model"] == "requested_model"
    assert parsed["usage"].input_tokens == 0


def test_token_bucket_refills_waits_and_times_out():
    clock = FakeClock()
    bucket = TokenBucket(60, clock.time, clock.sleep)
    assert bucket.acquire(60, timeout_seconds=1) == 0
    assert bucket.acquire(5, timeout_seconds=10) == pytest.approx(5.0)
    with pytest.raises(RateLimitTimeoutError):
        bucket.acquire(30, timeout_seconds=1)
    bucket.adjust(-10)
    assert bucket.available() == pytest.approx(10.0)
    assert bucket.acquire(1000, timeout_seconds=100) > 0


def test_circuit_breaker_transitions():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=clock.time)
    breaker.record_failure()
    breaker.before_call()
    breaker.record_failure()
    assert breaker.state == "open"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    clock.now += 10
    breaker.before_call()
    assert breaker.state == "half_open"
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    breaker.record_failure()
    assert breaker.state == "open"
    clock.now += 10
    breaker.before_call()
    breaker.record_success()
    assert breaker.state == "closed"


def test_retry_delay_bounds():
    generator = random.Random(1)
    delays = [retry_delay(attempt, None, 0.5, 4.0, generator) for attempt in range(8)]
    assert all(0 <= delay <= 4.0 for delay in delays)
    assert retry_delay(0, 9.0, 0.5, 4.0, generator) == 9.0


def test_registry_resolves_names_and_urls(tmp_path):
    endpoint = EndpointConfig(name="judge_api", base_url="https://judge.example.com/v1/", allowed_models=("j1",))
    registry = EndpointRegistry([endpoint])
    assert registry.resolve("judge_api") is endpoint
    assert registry.resolve("https://judge.example.com/v1") is endpoint
    with pytest.raises(EndpointNotFoundError):
        registry.resolve("http://unknown.example.com")
    with pytest.raises(ValueError):
        EndpointRegistry([endpoint, endpoint])
    with pytest.raises(ValueError):
        EndpointConfig(name="judge_api", base_url="https://x", allowed_models=("judge_latest",))
    registry_path = tmp_path / "endpoints.yaml"
    registry_path.write_text("endpoints:\n"
                             "  - {name: local, base_url: 'http://localhost:8000/v1', allowed_models: [m1]}\n")
    assert load_endpoint_registry(registry_path).resolve("local").allowed_models == ("m1",)


def test_environment_secret_store(monkeypatch):
    monkeypatch.setenv("RLWORLD_SECRET_JUDGE_KEY", "abc")
    assert EnvironmentSecretStore().get("judge-key") == "abc"
    with pytest.raises(MissingSecretError):
        EnvironmentSecretStore().get("absent")

