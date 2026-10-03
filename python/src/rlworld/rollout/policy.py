"""Policy calls through the model gateway (DESIGN.md sections 5 and 6.8).

One call per turn with the run's pinned model and sampling settings. Training
endpoints are asked for exact token ids and logprobs; eval runs read and write
the response cache so a rerun replays the same answers. The reply is put into
the shape the run's action adapter parses: text for text actions, tool calls
with decoded inputs for the computer tool, output items for typed actions.
"""

import json
from collections.abc import Sequence
from typing import Any, Literal, Protocol

from rlworld.actions.adapters import ActionParseError, ModelAdapter
from rlworld.actions.geometry import ScreenGeometry
from rlworld.actions.models import Action
from rlworld.gateway.messages import ChatRequest, ChatResponse
from rlworld.specs.models import EndpointSpec

CACHE_MODES = {"rl": "off", "eval": "read_write", "data": "off"}


class PolicyGateway(Protocol):
    def complete(self, request: ChatRequest) -> ChatResponse: ...


def tool_input(tool_call: dict[str, Any]) -> Any:
    if "input" in tool_call:
        return tool_call["input"]
    arguments = tool_call.get("arguments") or {}
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError as error:
        raise ActionParseError(f"{tool_call.get('name')}: arguments are not valid JSON") from error


def adapter_output(format_name: str, response: ChatResponse) -> Any:
    if format_name == "text_actions":
        return response.text
    if format_name == "computer_tool":
        return [{"name": tool_call.get("name"), "input": tool_input(tool_call)} for tool_call in response.tool_calls]
    return [
        tool_call if "type" in tool_call
        else {"type": "function_call", "name": tool_call.get("name"), "arguments": tool_call.get("arguments", {})}
        for tool_call in response.tool_calls
    ]


def assistant_reply(response: ChatResponse) -> str:
    """The model's turn as it goes back into the history."""
    call_lines = [json.dumps(tool_call, sort_keys=True, ensure_ascii=False) for tool_call in response.tool_calls]
    return "\n".join([response.text, *call_lines] if response.text else call_lines)


class PolicyClient:
    def __init__(
        self,
        gateway: PolicyGateway,
        endpoint: EndpointSpec,
        adapter: ModelAdapter,
        geometry: ScreenGeometry,
        run_id: str,
        run_type: Literal["rl", "eval", "data"],
    ) -> None:
        self.gateway = gateway
        self.endpoint = endpoint
        self.adapter = adapter
        self.geometry = geometry
        self.run_id = run_id
        self.run_type = run_type

    def tool_definitions(self, allowed_modalities: Sequence[str]) -> list[dict[str, Any]]:
        return self.adapter.tool_definitions(allowed_modalities, self.geometry)

    def complete(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], sample_index: int
    ) -> ChatResponse:
        return self.gateway.complete(
            ChatRequest(
                endpoint=self.endpoint.endpoint,
                model=self.endpoint.model,
                messages=tuple(messages),
                tools=tuple(tools),
                temperature=self.endpoint.temperature,
                max_tokens=self.endpoint.max_tokens,
                require_token_ids=self.endpoint.token_ids,
                cache_mode=CACHE_MODES[self.run_type],
                sample_index=sample_index,
                run_id=self.run_id,
                purpose="policy",
            )
        )

    def parse(self, response: ChatResponse) -> list[Action]:
        actions = self.adapter.parse_output(adapter_output(self.adapter.format_name, response), self.geometry)
        if not actions:
            raise ActionParseError("no action found in model output")
        return actions
