"""Single computer tool format used by frontier computer use APIs.

The model sees a downscaled screenshot, answers with one tool call per action,
uses pixel coordinates on that screenshot and xdotool style key strings. Non
computer actions travel as plain function tools.
"""

from collections.abc import Sequence
from typing import Any

from rlworld.actions.adapters.base import (
    ACTION_DESCRIPTIONS,
    ActionParseError,
    ActionRenderError,
    build_action,
    ensure_supported,
    env_point,
    function_action_classes,
    function_call_arguments,
    function_parameters_schema,
    parse_function_call,
    scroll_direction,
)
from rlworld.actions.geometry import ScreenGeometry, fit_within
from rlworld.actions.keys import render_xdotool_combo
from rlworld.actions.models import (
    Action,
    ActionModel,
    ClickAction,
    DragAction,
    KeyAction,
    MoveAction,
    ScrollAction,
    TypeAction,
    WaitAction,
    action_name,
)

COMPUTER_TOOL_NAME = "computer"
CLICK_KINDS = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}
CLICK_KIND_BY_BUTTON_AND_COUNT = {button_and_count: kind for kind, button_and_count in CLICK_KINDS.items()}
SCROLL_UNITS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


class ComputerToolAdapter:
    format_name = "computer_tool"
    supported_modalities = frozenset({"computer", "browser", "terminal", "native", "control"})

    def __init__(self, max_width: int = 1280, max_height: int = 800) -> None:
        self.max_width = max_width
        self.max_height = max_height

    def model_geometry(self, env_width: int, env_height: int) -> ScreenGeometry:
        model_width, model_height = fit_within(env_width, env_height, self.max_width, self.max_height)
        return ScreenGeometry(env_width, env_height, model_width, model_height)

    def tool_definitions(self, allowed_modalities: Sequence[str], geometry: ScreenGeometry) -> list[dict[str, Any]]:
        ensure_supported(self, allowed_modalities)
        definitions: list[dict[str, Any]] = []
        if "computer" in allowed_modalities:
            definitions.append(
                {
                    "name": COMPUTER_TOOL_NAME,
                    "type": "computer",
                    "display_width_px": geometry.model_width,
                    "display_height_px": geometry.model_height,
                }
            )
        for action_class in function_action_classes(allowed_modalities):
            function_name = action_name(action_class)
            definitions.append(
                {
                    "name": function_name,
                    "description": ACTION_DESCRIPTIONS[function_name],
                    "input_schema": function_parameters_schema(action_class),
                }
            )
        return definitions

    def parse_output(self, raw_output: Any, geometry: ScreenGeometry) -> list[Action]:
        if not isinstance(raw_output, list):
            raise ActionParseError("computer_tool output must be a list of tool calls")
        return [self.parse_tool_call(tool_call, geometry) for tool_call in raw_output]

    def parse_tool_call(self, tool_call: Any, geometry: ScreenGeometry) -> Action:
        if not isinstance(tool_call, dict) or "name" not in tool_call:
            raise ActionParseError(f"malformed tool call {tool_call!r}")
        tool_input = tool_call.get("input") or {}
        if tool_call["name"] == COMPUTER_TOOL_NAME:
            return self.parse_computer_input(tool_input, geometry)
        return parse_function_call(tool_call["name"], tool_input)

    def parse_computer_input(self, tool_input: dict[str, Any], geometry: ScreenGeometry) -> Action:
        kind = tool_input.get("action")
        try:
            if kind in CLICK_KINDS:
                button, count = CLICK_KINDS[kind]
                x, y = env_point(tool_input["coordinate"], geometry)
                modifiers = tool_input.get("text") or ()
                return build_action(
                    {"name": "click", "x": x, "y": y, "button": button, "count": count, "modifiers": modifiers}
                )
            if kind == "mouse_move":
                x, y = env_point(tool_input["coordinate"], geometry)
                return build_action({"name": "move", "x": x, "y": y})
            if kind == "left_click_drag":
                start_x, start_y = env_point(tool_input["start_coordinate"], geometry)
                end_x, end_y = env_point(tool_input["coordinate"], geometry)
                path = [{"x": start_x, "y": start_y}, {"x": end_x, "y": end_y}]
                return build_action({"name": "drag", "path": path})
            if kind == "scroll":
                return self.parse_scroll(tool_input, geometry)
            if kind == "type":
                return build_action({"name": "type", "text": tool_input["text"]})
            if kind == "key":
                return build_action({"name": "key", "keys": tool_input["text"]})
            if kind == "wait":
                return build_action({"name": "wait", "duration_ms": round(float(tool_input["duration"]) * 1000)})
            if kind == "screenshot":
                return build_action({"name": "wait", "duration_ms": 0})
        except KeyError as error:
            raise ActionParseError(f"computer action {kind!r} is missing {error.args[0]!r}") from error
        raise ActionParseError(f"unsupported computer action {kind!r}")

    def parse_scroll(self, tool_input: dict[str, Any], geometry: ScreenGeometry) -> Action:
        direction = tool_input["scroll_direction"]
        if direction not in SCROLL_UNITS:
            raise ActionParseError(f"unknown scroll direction {direction!r}")
        x, y = env_point(tool_input["coordinate"], geometry)
        notches = int(tool_input.get("scroll_amount", 3))
        unit_x, unit_y = SCROLL_UNITS[direction]
        return build_action(
            {"name": "scroll", "x": x, "y": y, "delta_x": unit_x * notches, "delta_y": unit_y * notches}
        )

    def render_action(self, action: ActionModel, geometry: ScreenGeometry) -> dict[str, Any]:
        if action.modality != "computer":
            return {"name": action.name, "input": function_call_arguments(action)}
        return {"name": COMPUTER_TOOL_NAME, "input": self.render_computer_input(action, geometry)}

    def render_computer_input(self, action: ActionModel, geometry: ScreenGeometry) -> dict[str, Any]:
        match action:
            case ClickAction():
                kind = CLICK_KIND_BY_BUTTON_AND_COUNT.get((action.button, action.count))
                if kind is None:
                    raise ActionRenderError(
                        f"computer_tool cannot express a {action.button} click with count {action.count}"
                    )
                rendered = {"action": kind, "coordinate": list(geometry.to_model(action.x, action.y))}
                if action.modifiers:
                    rendered["text"] = render_xdotool_combo(action.modifiers)
                return rendered
            case MoveAction():
                return {"action": "mouse_move", "coordinate": list(geometry.to_model(action.x, action.y))}
            case DragAction():
                if len(action.path) != 2 or action.button != "left":
                    raise ActionRenderError("computer_tool drags are straight left button drags")
                start_point, end_point = action.path
                return {
                    "action": "left_click_drag",
                    "start_coordinate": list(geometry.to_model(start_point.x, start_point.y)),
                    "coordinate": list(geometry.to_model(end_point.x, end_point.y)),
                }
            case ScrollAction():
                direction, notches = scroll_direction(action.delta_x, action.delta_y)
                return {
                    "action": "scroll",
                    "coordinate": list(geometry.to_model(action.x, action.y)),
                    "scroll_direction": direction,
                    "scroll_amount": notches,
                }
            case TypeAction():
                return {"action": "type", "text": action.text}
            case KeyAction():
                return {"action": "key", "text": render_xdotool_combo(action.keys)}
            case WaitAction():
                if action.duration_ms == 0:
                    return {"action": "screenshot"}
                return {"action": "wait", "duration": action.duration_ms / 1000}
        raise ActionRenderError(f"computer_tool cannot render {action.name}")
