"""Text actions in the UI TARS and Qwen VL style.

The model writes an optional Thought line, then an Action line such as
click(start_box='(966,546)'). Coordinates are absolute pixels on the smart
resized screenshot, or a 1000 by 1000 relative grid for older checkpoints.
Calls are parsed with the ast module and never evaluated.
"""

import ast
import re
from collections.abc import Sequence
from typing import Any, Literal

from rlworld.actions.adapters.base import (
    ActionParseError,
    ActionRenderError,
    build_action,
    ensure_supported,
    scroll_direction,
)
from rlworld.actions.geometry import QWEN_MAX_PIXELS, QWEN_MIN_PIXELS, QWEN_PATCH_FACTOR, ScreenGeometry, smart_resize
from rlworld.actions.keys import render_text_combo
from rlworld.actions.models import (
    Action,
    ActionModel,
    AnswerAction,
    AskUserAction,
    ClickAction,
    DoneAction,
    DragAction,
    KeyAction,
    MoveAction,
    ScrollAction,
    TypeAction,
    WaitAction,
)

BOX_TOKEN_PATTERN = re.compile(r"<\|box_(?:start|end)\|>")
THOUGHT_PATTERN = re.compile(r"Thought:(.*?)(?=\n\s*Action:|\Z)", re.DOTALL)
NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")
ACTION_MARKER = "Action:"
RELATIVE_GRID_SIZE = 1000
CLICK_FUNCTIONS = {"click": ("left", 1), "left_double": ("left", 2), "right_single": ("right", 1)}
CLICK_FUNCTION_BY_BUTTON_AND_COUNT = {button_and_count: name for name, button_and_count in CLICK_FUNCTIONS.items()}
SCROLL_UNITS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
ESCAPES_IN_QUOTES = {"\n": "\\n", "\r": "\\r"}
ACTION_SPACE_LINES = {
    "computer": [
        "click(start_box='(x,y)')",
        "left_double(start_box='(x,y)')",
        "right_single(start_box='(x,y)')",
        "hover(start_box='(x,y)')",
        "drag(start_box='(x1,y1)', end_box='(x2,y2)')",
        "hotkey(key='ctrl c')",
        "type(content='text')  # end with \\n to submit",
        "scroll(start_box='(x,y)', direction='down or up or left or right')",
        "wait()  # sleep, then observe again",
    ],
    "control": [
        "finished(content='answer')  # content is optional",
        "call_user()  # ask the user for help; explain why in your Thought",
    ],
}


class TextActionAdapter:
    format_name = "text_actions"
    supported_modalities = frozenset({"computer", "control"})

    def __init__(
        self,
        coordinate_mode: Literal["absolute_resized", "relative_1000"] = "absolute_resized",
        scroll_notches: int = 5,
        wait_ms: int = 5000,
        factor: int = QWEN_PATCH_FACTOR,
        min_pixels: int = QWEN_MIN_PIXELS,
        max_pixels: int = QWEN_MAX_PIXELS,
    ) -> None:
        self.coordinate_mode = coordinate_mode
        self.scroll_notches = scroll_notches
        self.wait_ms = wait_ms
        self.factor = factor
        self.min_pixels = min_pixels
        self.max_pixels = max_pixels

    def model_geometry(self, env_width: int, env_height: int) -> ScreenGeometry:
        if self.coordinate_mode == "relative_1000":
            return ScreenGeometry(env_width, env_height, RELATIVE_GRID_SIZE, RELATIVE_GRID_SIZE)
        model_width, model_height = smart_resize(env_width, env_height, self.factor, self.min_pixels, self.max_pixels)
        return ScreenGeometry(env_width, env_height, model_width, model_height)

    def tool_definitions(self, allowed_modalities: Sequence[str], geometry: ScreenGeometry) -> list[dict[str, Any]]:
        ensure_supported(self, allowed_modalities)
        action_lines = []
        for modality in ("computer", "control"):
            if modality in allowed_modalities:
                action_lines.extend(ACTION_SPACE_LINES[modality])
        return [
            {
                "type": "text_action_space",
                "screen_width": geometry.model_width,
                "screen_height": geometry.model_height,
                "actions": action_lines,
            }
        ]

    def parse_output(self, raw_output: Any, geometry: ScreenGeometry) -> list[Action]:
        if not isinstance(raw_output, str):
            raise ActionParseError("text_actions output must be a string")
        cleaned_text = BOX_TOKEN_PATTERN.sub("", raw_output)
        thought_match = THOUGHT_PATTERN.search(cleaned_text)
        thought = thought_match.group(1).strip() if thought_match else ""
        if ACTION_MARKER in cleaned_text:
            action_text = cleaned_text.split(ACTION_MARKER, 1)[1]
        else:
            action_text = THOUGHT_PATTERN.sub("", cleaned_text)
        call_texts = [call_text.strip() for call_text in split_calls(action_text) if call_text.strip()]
        if not call_texts:
            raise ActionParseError("no action found in model output")
        return [self.parse_call(call_text, thought, geometry) for call_text in call_texts]

    def parse_call(self, call_text: str, thought: str, geometry: ScreenGeometry) -> Action:
        function_name, arguments = parse_call_expression(call_text)
        try:
            if function_name in CLICK_FUNCTIONS:
                button, count = CLICK_FUNCTIONS[function_name]
                x, y = geometry.to_env(*parse_box(arguments["start_box"]))
                return build_action({"name": "click", "x": x, "y": y, "button": button, "count": count})
            if function_name in ("hover", "mouse_move"):
                x, y = geometry.to_env(*parse_box(arguments["start_box"]))
                return build_action({"name": "move", "x": x, "y": y})
            if function_name in ("drag", "select"):
                start_x, start_y = geometry.to_env(*parse_box(arguments["start_box"]))
                end_x, end_y = geometry.to_env(*parse_box(arguments["end_box"]))
                return build_action({"name": "drag", "path": [{"x": start_x, "y": start_y}, {"x": end_x, "y": end_y}]})
            if function_name in ("hotkey", "press"):
                return build_action({"name": "key", "keys": arguments["key"]})
            if function_name == "type":
                return build_action({"name": "type", "text": arguments["content"]})
            if function_name == "scroll":
                return self.parse_scroll(arguments, geometry)
            if function_name == "wait":
                return build_action({"name": "wait", "duration_ms": self.wait_ms})
            if function_name == "finished":
                content = arguments.get("content", "")
                return build_action({"name": "answer", "text": content} if content else {"name": "done"})
            if function_name == "call_user":
                return build_action({"name": "ask_user", "question": thought})
        except KeyError as error:
            raise ActionParseError(f"{function_name} is missing {error.args[0]!r}") from error
        raise ActionParseError(f"unknown action {function_name!r}")

    def parse_scroll(self, arguments: dict[str, Any], geometry: ScreenGeometry) -> Action:
        direction = arguments.get("direction", "down")
        if direction not in SCROLL_UNITS:
            raise ActionParseError(f"unknown scroll direction {direction!r}")
        x, y = geometry.to_env(*parse_box(arguments["start_box"]))
        unit_x, unit_y = SCROLL_UNITS[direction]
        return build_action(
            {
                "name": "scroll",
                "x": x,
                "y": y,
                "delta_x": unit_x * self.scroll_notches,
                "delta_y": unit_y * self.scroll_notches,
            }
        )

    def render_action(self, action: ActionModel, geometry: ScreenGeometry) -> str:
        match action:
            case ClickAction():
                function_name = CLICK_FUNCTION_BY_BUTTON_AND_COUNT.get((action.button, action.count))
                if function_name is None or action.modifiers:
                    raise ActionRenderError(f"text_actions cannot express this click: {action.model_dump()}")
                return f"{function_name}(start_box={quote_text(format_point(geometry, action.x, action.y))})"
            case MoveAction():
                return f"hover(start_box={quote_text(format_point(geometry, action.x, action.y))})"
            case DragAction():
                if len(action.path) != 2 or action.button != "left":
                    raise ActionRenderError("text_actions drags are straight left button drags")
                start_point, end_point = action.path
                start_box = quote_text(format_point(geometry, start_point.x, start_point.y))
                end_box = quote_text(format_point(geometry, end_point.x, end_point.y))
                return f"drag(start_box={start_box}, end_box={end_box})"
            case ScrollAction():
                direction = scroll_direction(action.delta_x, action.delta_y)[0]
                start_box = quote_text(format_point(geometry, action.x, action.y))
                return f"scroll(start_box={start_box}, direction={quote_text(direction)})"
            case TypeAction():
                return f"type(content={quote_text(action.text)})"
            case KeyAction():
                return f"hotkey(key={quote_text(render_text_combo(action.keys))})"
            case WaitAction():
                return "wait()"
            case AnswerAction():
                return f"finished(content={quote_text(action.text)})"
            case DoneAction():
                return "finished()"
            case AskUserAction():
                return "call_user()"
        raise ActionRenderError(f"text_actions cannot render {action.name}")


def split_calls(action_text: str) -> list[str]:
    """Split on newlines outside quotes; newlines inside quotes become escapes."""
    calls: list[str] = []
    current_characters: list[str] = []
    open_quote = ""
    is_escaped = False
    for character in action_text:
        if open_quote:
            if is_escaped:
                is_escaped = False
            elif character == "\\":
                is_escaped = True
            elif character == open_quote:
                open_quote = ""
            current_characters.append(ESCAPES_IN_QUOTES.get(character, character))
            continue
        if character in ("'", '"'):
            open_quote = character
        if character == "\n":
            calls.append("".join(current_characters))
            current_characters = []
            continue
        current_characters.append(character)
    calls.append("".join(current_characters))
    return calls


def parse_call_expression(call_text: str) -> tuple[str, dict[str, Any]]:
    try:
        expression = ast.parse(call_text, mode="eval").body
    except SyntaxError as error:
        raise ActionParseError(f"cannot parse action {call_text!r}") from error
    if not isinstance(expression, ast.Call) or not isinstance(expression.func, ast.Name) or expression.args:
        raise ActionParseError(f"actions must look like name(key='value'), got {call_text!r}")
    arguments: dict[str, Any] = {}
    for keyword in expression.keywords:
        if keyword.arg is None:
            raise ActionParseError(f"actions take named arguments only, got {call_text!r}")
        try:
            arguments[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError as error:
            raise ActionParseError(f"argument {keyword.arg} must be a literal in {call_text!r}") from error
    return expression.func.id, arguments


def parse_box(box_value: Any) -> tuple[float, float]:
    """A point "(x,y)" or a box "(x1,y1,x2,y2)" whose center is clicked."""
    numbers = [float(number) for number in NUMBER_PATTERN.findall(str(box_value))]
    if len(numbers) == 2:
        return numbers[0], numbers[1]
    if len(numbers) == 4:
        return (numbers[0] + numbers[2]) / 2, (numbers[1] + numbers[3]) / 2
    raise ActionParseError(f"a box needs 2 or 4 numbers, got {box_value!r}")


def format_point(geometry: ScreenGeometry, env_x: int, env_y: int) -> str:
    model_x, model_y = geometry.to_model(env_x, env_y)
    return f"({model_x},{model_y})"


def quote_text(text: str) -> str:
    escaped_text = (
        text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    )
    return f"'{escaped_text}'"
