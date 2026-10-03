"""Shared fakes for gateway tests: a fake clock, a scripted upstream and request builders."""

import random

import httpx

from rlworld.gateway import (
    ChatRequest,
    EndpointConfig,
    EndpointRegistry,
    ModelGateway,
    OpenAICompatibleTransport,
    StaticSecretStore,
)

POLICY_MODEL = "policy_model_v3"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeUpstream:
    """httpx mock handler that replays scripted responses and records every request."""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        scripted_response = self.responses.pop(0)
        if isinstance(scripted_response, Exception):
            raise scripted_response
        return scripted_response


def completion(text="OK", model=POLICY_MODEL, tool_calls=(), with_token_ids=False, prompt_tokens=100,
               completion_tokens=5, cached_tokens=0, headers=None) -> httpx.Response:
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = [
            {"id": f"call_{index}", "type": "function", "function": {"name": name, "arguments": arguments}}
            for index, (name, arguments) in enumerate(tool_calls)
        ]
    choice = {"index": 0, "message": message, "finish_reason": "stop"}
    payload = {
        "model": model,
        "choices": [choice],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        },
    }
    if with_token_ids:
        payload["prompt_token_ids"] = [1, 2, 3]
        choice["token_ids"] = [7, 8]
        choice["logprobs"] = {"content": [{"token": "O", "logprob": -0.1}, {"token": "K", "logprob": -0.2}]}
    return httpx.Response(200, json=payload, headers=headers)


def error_response(status_code: int, headers=None) -> httpx.Response:
    return httpx.Response(status_code, json={"error": {"message": "upstream trouble"}}, headers=headers)


def policy_endpoint(**overrides) -> EndpointConfig:
    settings = {
        "name": "policy_vllm",
        "base_url": "http://vllm.internal/v1",
        "allowed_models": (POLICY_MODEL,),
        "api_key_secret": "vllm_key",
        "provider_kind": "self_hosted",
        "returns_token_ids": True,
        "max_attempts": 3,
        "input_price_per_million_usd": 1.0,
        "output_price_per_million_usd": 2.0,
    }
    settings.update(overrides)
    return EndpointConfig(**settings)


def chat_request(**overrides) -> ChatRequest:
    settings = {
        "endpoint": "policy_vllm",
        "model": POLICY_MODEL,
        "messages": ({"role": "user", "content": "Open the workbook."},),
        "run_id": "run_1",
    }
    settings.update(overrides)
    return ChatRequest(**settings)


def build_gateway(upstream, endpoints=None, clock=None, **options):
    fake_clock = clock or FakeClock()
    transport = OpenAICompatibleTransport(httpx.Client(transport=httpx.MockTransport(upstream)))
    gateway = ModelGateway(
        EndpointRegistry(endpoints or [policy_endpoint()]),
        transport,
        StaticSecretStore({"vllm_key": "secret_123"}),
        clock=fake_clock.time,
        sleep=fake_clock.sleep,
        random_generator=random.Random(0),
        **options,
    )
    return gateway, fake_clock

