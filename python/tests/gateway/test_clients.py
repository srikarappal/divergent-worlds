import pytest

from rlworld.gateway import EndpointRegistry
from rlworld.gateway.clients import PROBE_PROMPT, GatewayEndpointProbe, GatewayJudgeClient
from rlworld.gateway.config import EndpointConfig
from rlworld.gateway.errors import TokenIdsUnavailableError, UpstreamError
from rlworld.gateway.messages import ChatResponse, TokenTrace
from rlworld.judge.prompt import JudgeRequest
from rlworld.specs.models import EndpointSpec

POLICY = EndpointConfig(name="vllm_policy", base_url="http://vllm.internal/v1", allowed_models=("policy_7b_v3",),
                        provider_kind="self_hosted", returns_token_ids=True, tokens_per_minute=600_000)
FRONTIER = EndpointConfig(name="frontier", base_url="https://api.frontier.example/v1", allowed_models=("judge_v2",),
                          provider_kind="frontier")


class AnsweringGateway:
    """Records requests and answers each with the next scripted reply or error."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def response(text="ok", trace=None):
    return ChatResponse(text=text, served_model="m", latency_seconds=0.42, token_trace=trace)


def test_judge_requests_go_through_the_gateway_as_judge_calls():
    gateway = AnsweringGateway(response('{"items": []}'))
    client = GatewayJudgeClient(gateway, "frontier", run_id="run_7", max_tokens=512)
    schema = {"type": "object"}
    request = JudgeRequest(model="judge_v2", messages=({"role": "user", "content": "grade"},), response_schema=schema,
                           sample_index=2, temperature=0.7)
    assert client.complete(request) == '{"items": []}'
    sent = gateway.requests[0]
    assert (sent.endpoint, sent.model, sent.purpose, sent.run_id, sent.max_tokens) == (
        "frontier", "judge_v2", "judge", "run_7", 512)
    assert (sent.response_schema, sent.sample_index, sent.temperature) == (schema, 2, 0.7)


def test_probes_send_one_token_and_report_what_came_back():
    trace = TokenTrace(prompt_token_ids=(1, 2), completion_token_ids=(3,), completion_logprobs=(-0.2,))
    gateway = AnsweringGateway(response(trace=trace))
    probe = GatewayEndpointProbe(gateway, EndpointRegistry([POLICY, FRONTIER]))
    result = probe.probe(EndpointSpec(endpoint="vllm_policy", model="policy_7b_v3", token_ids=True, logprobs=True))
    assert result.reachable and result.returns_token_ids and result.returns_logprobs
    assert (result.latency_seconds, result.rate_limit_tokens_per_minute, result.provider_kind) == (
        0.42, 600_000, "self_hosted")
    sent = gateway.requests[0]
    assert (sent.max_tokens, sent.purpose, sent.require_token_ids) == (1, "probe", True)
    assert sent.messages == ({"role": "user", "content": PROBE_PROMPT},)


@pytest.mark.parametrize(("reply", "reachable", "error"), [
    (TokenIdsUnavailableError("vllm_policy returned no token ids"), True, "returned no token ids"),
    (UpstreamError("502 bad gateway"), False, "502 bad gateway"),
])
def test_probe_failures_say_whether_the_endpoint_answered(reply, reachable, error):
    probe = GatewayEndpointProbe(AnsweringGateway(reply), EndpointRegistry([POLICY]))
    result = probe.probe(EndpointSpec(endpoint="vllm_policy", model="policy_7b_v3", token_ids=True))
    assert (result.reachable, result.returns_token_ids, result.provider_kind) == (reachable, False, "self_hosted")
    assert error in result.error


def test_unregistered_endpoints_are_unreachable_without_a_call():
    gateway = AnsweringGateway()
    result = GatewayEndpointProbe(gateway, EndpointRegistry([])).probe(EndpointSpec(endpoint="nowhere", model="x_1"))
    assert not result.reachable and "nowhere" in result.error and gateway.requests == []
    plain = AnsweringGateway(response())
    assert not GatewayEndpointProbe(plain, EndpointRegistry([FRONTIER])).probe(
        EndpointSpec(endpoint="frontier", model="judge_v2")).returns_token_ids
    assert plain.requests[0].require_token_ids is False
