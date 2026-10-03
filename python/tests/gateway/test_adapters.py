import json

from support.gateway_fakes import POLICY_MODEL, FakeUpstream, build_gateway, completion, error_response, policy_endpoint

from rlworld.gateway import EndpointRegistry, GatewayEndpointProbe, GatewayJudgeClient
from rlworld.judge import JudgeRequest
from rlworld.specs.models import EndpointSpec

JUDGE_REQUEST = JudgeRequest(
    model=POLICY_MODEL,
    messages=({"role": "system", "content": [{"type": "text", "text": "grade"}]},),
    response_schema={"type": "object"},
    sample_index=2,
    temperature=0.7,
)


def test_the_judge_client_sends_structured_requests():
    upstream = FakeUpstream([completion(text='{"items": []}')])
    gateway, _ = build_gateway(upstream)
    reply = GatewayJudgeClient(gateway, "policy_vllm", run_id="run_9").complete(JUDGE_REQUEST)
    assert reply == '{"items": []}'
    sent_payload = json.loads(upstream.requests[0].content)
    assert sent_payload["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert (sent_payload["temperature"], sent_payload["seed"]) == (0.7, 2)
    assert gateway.meter.records[0].purpose == "judge"
    assert gateway.meter.records[0].run_id == "run_9"


def test_the_probe_detects_token_ids_and_rate_limits():
    endpoint = policy_endpoint(tokens_per_minute=500_000)
    gateway, _ = build_gateway(FakeUpstream([completion(with_token_ids=True)]), endpoints=[endpoint])
    probe = GatewayEndpointProbe(gateway, EndpointRegistry([endpoint]))
    result = probe.probe(EndpointSpec(endpoint="http://vllm.internal/v1", model=POLICY_MODEL))
    assert result.reachable and result.returns_token_ids and result.returns_logprobs
    assert (result.rate_limit_tokens_per_minute, result.provider_kind) == (500_000, "self_hosted")


def test_the_probe_reports_unreachable_endpoints():
    endpoint = policy_endpoint(max_attempts=1)
    gateway, _ = build_gateway(FakeUpstream([error_response(503)]), endpoints=[endpoint])
    probe = GatewayEndpointProbe(gateway, EndpointRegistry([endpoint]))
    failing_result = probe.probe(EndpointSpec(endpoint="policy_vllm", model=POLICY_MODEL))
    assert not failing_result.reachable
    assert "503" in failing_result.error
    unknown_result = probe.probe(EndpointSpec(endpoint="http://nowhere.example.com", model=POLICY_MODEL))
    assert not unknown_result.reachable
    assert "no endpoint is registered" in unknown_result.error


def test_the_probe_flags_endpoints_without_token_ids():
    endpoint = policy_endpoint(returns_token_ids=False)
    gateway, _ = build_gateway(FakeUpstream([completion()]), endpoints=[endpoint])
    result = GatewayEndpointProbe(gateway, EndpointRegistry([endpoint])).probe(
        EndpointSpec(endpoint="policy_vllm", model=POLICY_MODEL)
    )
    assert result.reachable and not result.returns_token_ids
