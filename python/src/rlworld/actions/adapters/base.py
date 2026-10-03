"""Shared adapter pieces: errors, the adapter protocol and function tools for non computer actions."""

import json
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import ValidationError

from rlworld.actions.geometry import ScreenGeometry
from rlworld.actions.models import ACTION_CLASSES, Action, ActionModel, action_name, parse_action

FUNCTION_TOOL_MODALITIES = ("browser", "terminal", "native", "control")
FUNCTION_ACTION_CLASSES = tuple(
    action_class for action_class in ACTION_CLASSES if action_class.modality in FUNCTION_TOOL_MODALITIES
)
FUNCTION_ACTION_NAMES = frozenset(action_name(action_class) for action_class in FUNCTION_ACTION_CLASSES)
ACTION_DESCRIPTIONS = {
    "goto": "Open a URL in the current tab.",
    "click_element": "Click the element with this accessibility or DOM id.",
    "fill": "Replace the value of an input element.",
    "select_option": "Choose an option in a select element.",
    "back": "Go back one page in history.",
    "tab": "Open, close or switch browser tabs.",
    "exec": "Run a shell command; the next observation carries stdout, stderr and the exit code.",
    "native_call": "Call an application API method, such as an Excel range write.",
    "ask_user": "Ask the user a clarifying question; the reply arrives with the next observation.",
    "answer": "Submit the final answer and end the episode.",
    "done": "Declare the task complete and end the episode.",
    "give_up": "Stop because the task cannot be completed.",
}


class ActionParseError(ValueError):
    """Model output that cannot become canonical actions; scored as an agent error, never an env error."""


class ActionRenderError(ValueError):
    """A canonical action that this model format cannot express."""


class AdapterCapabilityError(ValueError):
    """The task allows modalities that this model format cannot express."""


class ModelAdapter(Protocol):
    format_name: str
    supported_modalities: frozenset[str]

    def model_geometry(self, env_width: int, env_height: int) -> ScreenGeometry: ...

    def tool_definitions(
        self, allowed_modalities: Sequence[str], geometry: ScreenGeometry
    ) -> list[dict[str, Any]]: ...

    def parse_output(self, raw_output: Any, geometry: ScreenGeometry) -> list[Action]: ...

    def render_action(self, action: ActionModel, geometry: ScreenGeometry) -> Any: ...


def ensure_supported(adapter: ModelAdapter, allowed_modalities: Sequence[str]) -> None:
    unsupported_modalities = sorted(set(allowed_modalities) - adapter.supported_modalities)
    if unsupported_modalities:
        raise AdapterCapabilityError(
            f"{adapter.format_name} cannot express {', '.join(unsupported_modalities)} actions"
        )


def function_action_classes(allowed_modalities: Sequence[str]) -> list[type[ActionModel]]:
    return [action_class for action_class in FUNCTION_ACTION_CLASSES if action_class.modality in allowed_modalities]


def function_parameters_schema(action_class: type[ActionModel]) -> dict[str, Any]:
    schema = action_class.model_json_schema()
    properties = {key: value for key, value in schema.get("properties", {}).items() if key != "name"}
    required = [key for key in schema.get("required", []) if key != "name"]
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def build_action(payload: dict[str, Any]) -> Action:
    try:
        return parse_action(payload)
    except ValidationError as error:
        first_problem = error.errors()[0]["msg"].removeprefix("Value error, ")
        raise ActionParseError(f"invalid {payload.get('name')} action: {first_problem}") from error


def parse_function_call(function_name: str, arguments: Any) -> Action:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as error:
            raise ActionParseError(f"{function_name}: arguments are not valid JSON") from error
    if not isinstance(arguments, dict):
        raise ActionParseError(f"{function_name}: arguments must be an object")
    if function_name not in FUNCTION_ACTION_NAMES:
        raise ActionParseError(f"unknown function {function_name!r}")
    return build_action({**arguments, "name": function_name})


def function_call_arguments(action: ActionModel) -> dict[str, Any]:
    return action.model_dump(mode="json", exclude={"name"})


def env_point(raw_point: Any, geometry: ScreenGeometry) -> tuple[int, int]:
    if not isinstance(raw_point, (list, tuple)) or len(raw_point) != 2:
        raise ActionParseError(f"a coordinate needs two numbers, got {raw_point!r}")
    try:
        model_x, model_y = float(raw_point[0]), float(raw_point[1])
    except (TypeError, ValueError) as error:
        raise ActionParseError(f"a coordinate needs two numbers, got {raw_point!r}") from error
    return geometry.to_env(model_x, model_y)


def scroll_direction(delta_x: int, delta_y: int) -> tuple[str, int]:
    """Single axis scroll as (direction, notches)."""
    if delta_x and delta_y:
        raise ActionRenderError("this format allows one scroll axis per action")
    if delta_y:
        return ("down", delta_y) if delta_y > 0 else ("up", -delta_y)
    return ("right", delta_x) if delta_x > 0 else ("left", -delta_x)
