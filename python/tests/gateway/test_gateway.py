import json

import httpx
import pytest
from support.gateway_fakes import (
    POLICY_MODEL,
    FakeUpstream,
    build_gateway,
    chat_request,
    completion,
    error_response,
    policy_endpoint,
)

from rlworld.gateway import (
    CacheMissError,
    CircuitOpenError,
    DataPolicyViolationError,
    FileResponseCache,
    InMemoryResponseCache,
    MissingSecretError,
    ModelNotPinnedError,
    RateLimitTimeoutError,
    TokenIdsUnavailableError,
    UpstreamError,
)


def test_a_successful_call_is_priced_and_metered():
    upstream = FakeUpstream([completion(tool_calls=[("exec", '{"command": "ls"}')])])
    gateway, _ = build_gateway(upstream)
    response = gateway.complete(chat_request())
    assert response.text == "OK"
    assert response.tool_calls == ({"name": "exec", "arguments": '{"command": "ls"}'},)
    assert (response.usage.input_tokens, response.usage.output_tokens) == (100, 5)
    assert response.cost_usd == pytest.approx((100 * 1.0 + 5 * 2.0) / 1e6)
    sent_request = upstream.requests[0]
    assert sent_request.url.path == "/v1/chat/completions"
    assert sent_request.headers["authorization"] == "Bearer secret_123"
    assert json.loads(sent_request.content)["model"] == POLICY_MODEL
    summary = gateway.meter.summary("run_1")["policy_vllm"]
    assert (summary.calls, summary.errors, summary.input_tokens) == (1, 0, 100)


def test_only_pinned_models_may_be_called():
    upstream = FakeUpstream([])
    gateway, _ = build_gateway(upstream)
    with pytest.raises(ModelNotPinnedError):
        gateway.complete(chat_request(model="policy_model_v4"))
    assert upstream.requests == []


def test_the_data_policy_blocks_screenshots():
    gateway, _ = build_gateway(FakeUpstream([]), endpoints=[policy_endpoint(accepts_screenshots=False)])
    image_message = {"role": "user", "content": [{"type": "image", "uri": "https://frames.example.com/1.webp"}]}
    with pytest.raises(DataPolicyViolationError):
        gateway.complete(chat_request(messages=(image_message,)))


def test_server_errors_are_retried_with_jitter():
    upstream = FakeUpstream([error_response(500), completion()])
    gateway, fake_clock = build_gateway(upstream)
    assert gateway.complete(chat_request()).attempts == 2
    assert len(fake_clock.sleeps) == 1
    assert 0 <= fake_clock.sleeps[0] <= 0.5


def test_retry_after_is_respected():
    upstream = FakeUpstream([error_response(429, headers={"retry-after": "3"}), completion()])
    gateway, fake_clock = build_gateway(upstream)
    gateway.complete(chat_request())
    assert fake_clock.sleeps[0] >= 3


def test_client_errors_are_not_retried():
    upstream = FakeUpstream([error_response(400)])
    gateway, _ = build_gateway(upstream)
    with pytest.raises(UpstreamError) as raised:
        gateway.complete(chat_request())
    assert raised.value.status_code == 400
    assert len(upstream.requests) == 1
    assert gateway.meter.summary()["policy_vllm"].errors == 1


def test_retries_stop_after_max_attempts():
    upstream = FakeUpstream([error_response(503), error_response(503), error_response(503)])
    gateway, _ = build_gateway(upstream)
    with pytest.raises(UpstreamError):
        gateway.complete(chat_request())
    assert len(upstream.requests) == 3


def test_timeouts_are_retried():
    upstream = FakeUpstream([httpx.ReadTimeout("slow"), completion()])
    gateway, _ = build_gateway(upstream)
    assert gateway.complete(chat_request()).attempts == 2


def test_the_circuit_opens_then_recovers():
    upstream = FakeUpstream([error_response(500), error_response(500), completion()])
    gateway, fake_clock = build_gateway(
        upstream, endpoints=[policy_endpoint(max_attempts=1)], circuit_failure_threshold=2, circuit_cooldown_seconds=30
    )
    for _ in range(2):
        with pytest.raises(UpstreamError):
            gateway.complete(chat_request())
    with pytest.raises(CircuitOpenError):
        gateway.complete(chat_request())
    assert len(upstream.requests) == 2
    assert gateway.headroom("policy_vllm").circuit_state == "open"
    fake_clock.now += 30
    assert gateway.complete(chat_request()).text == "OK"
    assert gateway.headroom("policy_vllm").circuit_state == "closed"


def test_rate_limits_wait_and_time_out():
    upstream = FakeUpstream([completion(), completion(), completion()])
    gateway, fake_clock = build_gateway(upstream, endpoints=[policy_endpoint(requests_per_minute=2)])
    gateway.complete(chat_request())
    gateway.complete(chat_request())
    assert gateway.headroom("policy_vllm").requests_available == pytest.approx(0.0)
    gateway.complete(chat_request())
    assert fake_clock.sleeps == [pytest.approx(30.0)]
    impatient_gateway, _ = build_gateway(
        FakeUpstream([completion(), completion()]), endpoints=[policy_endpoint(requests_per_minute=1)],
        wait_timeout_seconds=10,
    )
    impatient_gateway.complete(chat_request())
    with pytest.raises(RateLimitTimeoutError):
        impatient_gateway.complete(chat_request())


def test_eval_responses_are_cached():
    upstream = FakeUpstream([completion(text="first"), completion(text="fresh")])
    gateway, _ = build_gateway(upstream, response_cache=InMemoryResponseCache())
    first_response = gateway.complete(chat_request(cache_mode="read_write"))
    replayed_response = gateway.complete(chat_request(cache_mode="read_write"))
    assert (first_response.cached, replayed_response.cached) == (False, True)
    assert replayed_response.text == "first"
    assert gateway.complete(chat_request()).text == "fresh"
    assert len(upstream.requests) == 2
    with pytest.raises(CacheMissError):
        gateway.complete(chat_request(cache_mode="read_only", sample_index=1))


def test_file_caches_replay_across_gateways(tmp_path):
    first_gateway, _ = build_gateway(FakeUpstream([completion(text="recorded")]),
                                     response_cache=FileResponseCache(tmp_path))
    first_gateway.complete(chat_request(cache_mode="read_write"))
    replay_gateway, _ = build_gateway(FakeUpstream([]), response_cache=FileResponseCache(tmp_path))
    assert replay_gateway.complete(chat_request(cache_mode="read_only")).text == "recorded"


def test_training_requests_need_exact_tokens():
    gateway, _ = build_gateway(FakeUpstream([completion(with_token_ids=True), completion()]))
    traced_response = gateway.complete(chat_request(require_token_ids=True))
    assert traced_response.token_trace.prompt_token_ids == (1, 2, 3)
    assert traced_response.token_trace.completion_token_ids == (7, 8)
    assert traced_response.token_trace.completion_logprobs == (-0.1, -0.2)
    with pytest.raises(TokenIdsUnavailableError):
        gateway.complete(chat_request(require_token_ids=True))


def test_a_silently_swapped_model_is_flagged():
    gateway, _ = build_gateway(FakeUpstream([completion(model="policy_model_v3_hotfix")]))
    gateway.complete(chat_request())
    assert gateway.meter.records[0].model_mismatch
    assert gateway.meter.summary()["policy_vllm"].model_mismatches == 1


def test_missing_secrets_fail_before_any_call():
    upstream = FakeUpstream([])
    gateway, _ = build_gateway(upstream, endpoints=[policy_endpoint(api_key_secret="absent_key")])
    with pytest.raises(MissingSecretError):
        gateway.complete(chat_request())
    assert upstream.requests == []


def test_token_buckets_reconcile_with_real_usage():
    gateway, _ = build_gateway(
        FakeUpstream([completion(prompt_tokens=2000, completion_tokens=500)]),
        endpoints=[policy_endpoint(tokens_per_minute=100_000)],
    )
    gateway.complete(chat_request(max_tokens=100))
    assert gateway.headroom("policy_vllm").tokens_available == pytest.approx(100_000 - 2500)
