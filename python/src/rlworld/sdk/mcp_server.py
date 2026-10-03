"""MCP facade: drive one env as tools from any agent harness (DESIGN.md section 6.3).

A small synchronous JSON-RPC 2.0 server over stdio: initialize, tools/list and
tools/call. Each canonical action the task allows becomes a tool, plus observe.
Tool failures come back as results with isError set, the MCP convention.
"""

import base64
import json
from collections.abc import Callable
from typing import Any, TextIO

from pydantic import ValidationError

from rlworld.actions.adapters.base import function_parameters_schema
from rlworld.actions.models import ActionModel, Observation, action_classes_for, action_name, parse_action
from rlworld.sdk.env import ComputerUseEnv

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "rlworld_env", "version": "0.1.0"}
OBSERVE_TOOL = "observe"
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_PARAMS = -32602

ImageFetcher = Callable[[str], bytes]


def observation_summary(observation: Observation | dict[str, Any] | None) -> dict[str, Any]:
    if observation is None:
        return {}
    if isinstance(observation, Observation):
        return observation.model_dump(mode="json", exclude_none=True, exclude={"screenshot"})
    return {key: value for key, value in observation.items() if key != "screenshot"}


class EnvToolServer:
    def __init__(self, env: ComputerUseEnv, image_fetcher: ImageFetcher | None = None) -> None:
        self.env = env
        self.image_fetcher = image_fetcher
        self.action_classes = {
            action_name(action_class): action_class for action_class in action_classes_for(env.allowed_modalities)
        }

    def tool_definitions(self) -> list[dict[str, Any]]:
        definitions = [{"name": OBSERVE_TOOL, "description": "Return the current observation.",
                        "inputSchema": {"type": "object", "properties": {}}}]
        for name, action_class in self.action_classes.items():
            definitions.append({
                "name": name,
                "description": (action_class.__doc__ or f"Perform the {name} action.").strip(),
                "inputSchema": function_parameters_schema(action_class),
            })
        return definitions

    def observation_content(self, observation: Observation | dict[str, Any] | None) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(observation_summary(observation))}]
        screenshot = observation.screenshot if isinstance(observation, Observation) else None
        if screenshot is not None and self.image_fetcher is not None:
            content.append({
                "type": "image",
                "data": base64.b64encode(self.image_fetcher(screenshot.uri)).decode("ascii"),
                "mimeType": f"image/{screenshot.image_format}",
            })
        elif screenshot is not None:
            content.append({"type": "text", "text": f"screenshot: {screenshot.uri}"})
        return content

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == OBSERVE_TOOL:
            return {"content": self.observation_content(self.env.last_observation), "isError": False}
        if name not in self.action_classes:
            return {"content": [{"type": "text", "text": f"unknown or disallowed tool {name}"}], "isError": True}
        try:
            action: ActionModel = parse_action({**arguments, "name": name})
            observation, reward, terminated, truncated, info = self.env.step(action)
        except (ValidationError, ValueError, RuntimeError) as error:
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}
        content = self.observation_content(observation)
        if terminated or truncated:
            content.append({"type": "text", "text": json.dumps({"episode_finished": True, "reward": reward,
                                                                "truncated": truncated})})
        return {"content": content, "isError": False}

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """One JSON-RPC message in, one response out; notifications get no response."""
        method = message.get("method")
        message_id = message.get("id")
        if message_id is None:
            return None
        if method == "initialize":
            requested_version = (message.get("params") or {}).get("protocolVersion")
            version = requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": SERVER_INFO}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.tool_definitions()}
        elif method == "tools/call":
            params = message.get("params") or {}
            if "name" not in params:
                return jsonrpc_error(message_id, JSONRPC_INVALID_PARAMS, "tools/call needs a tool name")
            result = self.call_tool(params["name"], params.get("arguments") or {})
        else:
            return jsonrpc_error(message_id, JSONRPC_METHOD_NOT_FOUND, f"unknown method {method}")
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def serve(self, input_stream: TextIO, output_stream: TextIO) -> None:
        """Newline delimited JSON-RPC over stdio until the input closes."""
        for line in input_stream:
            if not line.strip():
                continue
            try:
                response = self.handle_message(json.loads(line))
            except json.JSONDecodeError as error:
                response = jsonrpc_error(None, JSONRPC_PARSE_ERROR, str(error))
            if response is not None:
                output_stream.write(json.dumps(response) + "\n")
                output_stream.flush()


def jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
