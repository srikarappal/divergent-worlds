"""Typed computer action items in the OpenAI computer use shape, plus function calls.

Each output item is a computer_call carrying one typed action, a function_call
for a non computer action, or reasoning and message text that carries no action.
Scrolls are in pixels and become wheel notches.
"""

import json
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
)
from rlworld.actions.geometry import ScreenGeometry, fit_within
from rlworld.actions.keys import render_typed_keys
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

BUTTONS_FROM_MODEL = {"left": "left", "right": "right", "wheel": "middle", "middle": "middle"}
BUTTONS_TO_MODEL = {"left": "left", "right": "right", "middle": "wheel"}
NON_ACTION_ITEM_TYPES = frozenset({"message", "reasoning", "output_text"})


class TypedActionAdapter:
    format_name = "typed_actions"
    supported_modalities = frozenset({"computer", "browser", "terminal", "native", "control"})

    def __init__(
        self,
        max_width: int = 1920,
        max_height: int = 1080,
        pixels_per_scroll_notch: int = 100,
        environment: str = "linux",
    ) -> None:
        self.max_width = max_width
        self.max_height = max_height
        self.pixels_per_scroll_notch = pixels_per_scroll_notch
        self.environment = environment

    def model_geometry(self, env_width: int, env_height: int) -> ScreenGeometry:
        model_width, model_height = fit_within(env_width, env_height, self.max_width, self.max_height)
        return ScreenGeometry(env_width, env_height, model_width, model_height)

    def tool_definitions(self, allowed_modalities: Sequence[str], geometry: ScreenGeometry) -> list[dict[str, Any]]:
        ensure_supported(self, allowed_modalities)
        definitions: list[dict[str, Any]] = []
        if "computer" in allowed_modalities:
            definitions.append(
                {
                    "type": "computer_use_preview",
                    "display_width": geometry.model_width,
                    "display_height": geometry.model_height,
                    "environment": self.environment,
                }
            )
        for action_class in function_action_classes(allowed_modalities):
            function_name = action_name(action_class)
            definitions.append(
                {
                    "type": "function",
                    "name": function_name,
                    "description": ACTION_DESCRIPTIONS[function_name],
                    "parameters": function_parameters_schema(action_class),
                }
            )
        return definitions

    def parse_output(self, raw_output: Any, geometry: ScreenGeometry) -> list[Action]:
        if not isinstance(raw_output, list):
            raise ActionParseError("typed_actions output must be a list of output items")
        actions = []
        for item in raw_output:
            item_type = item.get("type") if isinstance(item, dict) else None
            if item_type == "computer_call":
                actions.append(self.parse_computer_action(item.get("action") or {}, geometry))
            elif item_type == "function_call":
                actions.append(parse_function_call(item.get("name", ""), item.get("arguments", {})))
            elif item_type not in NON_ACTION_ITEM_TYPES:
                raise ActionParseError(f"unknown output item type {item_type!r}")
        return actions

    def parse_computer_action(self, typed_action: dict[str, Any], geometry: ScreenGeometry) -> Action:
        kind = typed_action.get("type")
        try:
            if kind in ("click", "double_click"):
                model_button = typed_action.get("button", "left")
                if model_button not in BUTTONS_FROM_MODEL:
                    raise ActionParseError(f"unsupported mouse button {model_button!r}")
                x, y = env_point((typed_action["x"], typed_action["y"]), geometry)
                count = 2 if kind == "double_click" else 1
                return build_action(
                    {"name": "click", "x": x, "y": y, "button": BUTTONS_FROM_MODEL[model_button], "count": count}
                )
            if kind == "move":
                x, y = env_point((typed_action["x"], typed_action["y"]), geometry)
                return build_action({"name": "move", "x": x, "y": y})
            if kind == "drag":
                path = [env_point((point["x"], point["y"]), geometry) for point in typed_action["path"]]
                return build_action({"name": "drag", "path": [{"x": x, "y": y} for x, y in path]})
            if kind == "scroll":
                x, y = env_point((typed_action["x"], typed_action["y"]), geometry)
                delta_x = self.pixels_to_notches(typed_action.get("scroll_x", 0))
                delta_y = self.pixels_to_notches(typed_action.get("scroll_y", 0))
                return build_action({"name": "scroll", "x": x, "y": y, "delta_x": delta_x, "delta_y": delta_y})
            if kind == "type":
                return build_action({"name": "type", "text": typed_action["text"]})
            if kind == "keypress":
                return build_action({"name": "key", "keys": typed_action["keys"]})
            if kind == "wait":
                return build_action({"name": "wait", "duration_ms": 1000})
            if kind == "screenshot":
                return build_action({"name": "wait", "duration_ms": 0})
        except KeyError as error:
            raise ActionParseError(f"computer action {kind!r} is missing {error.args[0]!r}") from error
        raise ActionParseError(f"unsupported computer action {kind!r}")

    def pixels_to_notches(self, pixels: float) -> int:
        """Round to wheel notches, but never swallow a small non zero scroll."""
        notches = round(pixels / self.pixels_per_scroll_notch)
        if notches == 0 and pixels:
            return 1 if pixels > 0 else -1
        return notches

    def render_action(self, action: ActionModel, geometry: ScreenGeometry) -> dict[str, Any]:
        if action.modality != "computer":
            arguments = json.dumps(function_call_arguments(action), sort_keys=True)
            return {"type": "function_call", "name": action.name, "arguments": arguments}
        return {"type": "computer_call", "action": self.render_computer_action(action, geometry)}

    def render_computer_action(self, action: ActionModel, geometry: ScreenGeometry) -> dict[str, Any]:
        match action:
            case ClickAction():
                if action.modifiers:
                    raise ActionRenderError("typed_actions clicks cannot carry modifier keys")
                x, y = geometry.to_model(action.x, action.y)
                if action.count == 2 and action.button == "left":
                    return {"type": "double_click", "x": x, "y": y}
                if action.count == 1:
                    return {"type": "click", "x": x, "y": y, "button": BUTTONS_TO_MODEL[action.button]}
                raise ActionRenderError(
                    f"typed_actions cannot express a {action.button} click with count {action.count}"
                )
            case MoveAction():
                x, y = geometry.to_model(action.x, action.y)
                return {"type": "move", "x": x, "y": y}
            case DragAction():
                path = [geometry.to_model(point.x, point.y) for point in action.path]
                return {"type": "drag", "path": [{"x": x, "y": y} for x, y in path]}
            case ScrollAction():
                x, y = geometry.to_model(action.x, action.y)
                return {
                    "type": "scroll",
                    "x": x,
                    "y": y,
                    "scroll_x": action.delta_x * self.pixels_per_scroll_notch,
                    "scroll_y": action.delta_y * self.pixels_per_scroll_notch,
                }
            case TypeAction():
                return {"type": "type", "text": action.text}
            case KeyAction():
                return {"type": "keypress", "keys": render_typed_keys(action.keys)}
            case WaitAction():
                return {"type": "screenshot"} if action.duration_ms == 0 else {"type": "wait"}
        raise ActionRenderError(f"typed_actions cannot render {action.name}")
