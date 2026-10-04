"""A BrowserGym compatible interface over one env (DESIGN.md section 6.3).

Agents written for BrowserGym drive an rlworld env as they drive BrowserGym:
they read its observation dict and answer with its high level action strings,
such as click('12') or fill('7', 'ACME'). Element ids are the ids DOM
observations carry, which follow BrowserGym's bid convention, so the DOM
snapshot here names them bid. Action strings are parsed with the ast module and
never evaluated; each call becomes one canonical action and a function with no
canonical counterpart is refused rather than approximated. send_msg_to_user
answers and ends the episode, report_infeasible gives up and scroll turns
pixels into wheel notches at the screen's center. An action the parser or the
task refuses is reported in last_action_error, as BrowserGym does.

The env observes no tab list or URL, so those keys stay empty; the accessibility
tree is the desktop's, whose nodes carry no bids; the screenshot is an RGB PIL
image, which numpy and PIL read like BrowserGym's array.
"""

import ast
import io
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Protocol

from PIL import Image

from rlworld.actions.accessibility import PrunedNode
from rlworld.actions.adapters.base import ActionParseError, ActionRenderError, build_action
from rlworld.actions.keys import render_playwright_combo
from rlworld.actions.models import (
    ELEMENT_ATTRIBUTE,
    Action,
    ActionModel,
    ActionNotAllowedError,
    AnswerAction,
    BackAction,
    ClickAction,
    DragAction,
    ElementClickAction,
    FillAction,
    GiveUpAction,
    GotoAction,
    KeyAction,
    MoveAction,
    Observation,
    ScrollAction,
    SelectOptionAction,
    TabAction,
    TypeAction,
    WaitAction,
)
from rlworld.gateway.openai_compatible import BLOB_SCHEME
from rlworld.sdk.env import ComputerUseEnv
from rlworld.sdk.experiments import ExperimentClient

BID_ATTRIBUTE = "bid"
DEFAULT_SCROLL_PIXELS_PER_NOTCH = 100
DEFAULT_NOOP_MS = 1000
# Parameter names in BrowserGym's order, so positional and keyword arguments bind the same way.
SIGNATURES: dict[str, tuple[str, ...]] = {
    "click": ("bid", "button", "modifiers"),
    "fill": ("bid", "value"),
    "select_option": ("bid", "options"),
    "clear": ("bid",),
    "scroll": ("delta_x", "delta_y"),
    "mouse_move": ("x", "y"),
    "mouse_click": ("x", "y", "button"),
    "mouse_dblclick": ("x", "y", "button"),
    "mouse_drag_and_drop": ("from_x", "from_y", "to_x", "to_y"),
    "keyboard_press": ("key",),
    "keyboard_type": ("text",),
    "keyboard_insert_text": ("text",),
    "goto": ("url",),
    "go_back": (),
    "new_tab": (),
    "tab_close": (),
    "tab_focus": ("index",),
    "send_msg_to_user": ("text",),
    "report_infeasible": ("reason",),
    "noop": ("wait_ms",),
}
NO_CANONICAL_ACTION = frozenset({"dblclick", "hover", "press", "focus", "drag_and_drop", "upload_file", "mouse_down",
                                 "mouse_up", "mouse_upload_file", "keyboard_down", "keyboard_up", "go_forward"})
DOCUMENT_NODE, ELEMENT_NODE, TEXT_NODE, COMMENT_NODE = 9, 1, 3, 8
VOID_ELEMENTS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
                           "track", "wbr"})


def literal(node: ast.expr, function_name: str) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError) as error:
        raise ActionParseError(f"{function_name} takes literal arguments only") from error


def call_arguments(call: ast.Call) -> tuple[str, dict[str, Any]]:
    """The function's name and its arguments by parameter name."""
    if not isinstance(call.func, ast.Name):
        raise ActionParseError("BrowserGym actions are plain function calls")
    name = call.func.id
    if name in NO_CANONICAL_ACTION:
        raise ActionParseError(f"{name} has no canonical action")
    if name not in SIGNATURES:
        raise ActionParseError(f"unknown BrowserGym action {name!r}")
    parameters = SIGNATURES[name]
    if len(call.args) > len(parameters):
        raise ActionParseError(f"{name} takes at most {len(parameters)} arguments")
    arguments = {parameter: literal(value, name) for parameter, value in zip(parameters, call.args)}
    for keyword in call.keywords:
        if keyword.arg not in parameters or keyword.arg in arguments:
            raise ActionParseError(f"{name} got an unexpected or repeated argument {keyword.arg!r}")
        arguments[keyword.arg] = literal(keyword.value, name)
    return name, arguments


def pixel(value: Any) -> int:
    """BrowserGym coordinates may be fractional; canonical ones are whole pixels."""
    try:
        return round(float(value))
    except (TypeError, ValueError) as error:
        raise ActionParseError(f"{value!r} is not a coordinate") from error


def single_option(options: Any) -> str:
    if isinstance(options, str):
        return options
    if isinstance(options, (list, tuple)) and len(options) == 1 and isinstance(options[0], str):
        return options[0]
    raise ActionParseError("select_option chooses exactly one option")


@dataclass(frozen=True)
class BrowserGymActions:
    """BrowserGym action strings to canonical actions and back; coordinates are screenshot pixels."""

    screen_width: int
    screen_height: int
    scroll_pixels_per_notch: int = DEFAULT_SCROLL_PIXELS_PER_NOTCH

    def parse(self, text: str) -> list[Action]:
        try:
            module = ast.parse(text.strip())
        except SyntaxError as error:
            raise ActionParseError(f"cannot parse {text!r}") from error
        actions = []
        for statement in module.body:
            if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
                raise ActionParseError("BrowserGym actions are function calls, one per line")
            name, arguments = call_arguments(statement.value)
            try:
                actions.append(build_action(self.payload(name, arguments)))
            except KeyError as error:
                raise ActionParseError(f"{name} is missing {error.args[0]!r}") from error
        if not actions:
            raise ActionParseError("no action found")
        return actions

    def notches(self, pixels: Any) -> int:
        try:
            amount = float(pixels)
        except (TypeError, ValueError) as error:
            raise ActionParseError(f"{pixels!r} is not a scroll amount") from error
        if amount == 0:
            return 0
        count = max(1, round(abs(amount) / self.scroll_pixels_per_notch))
        return count if amount > 0 else -count

    def payload(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "click":
            if arguments.get("button", "left") != "left" or arguments.get("modifiers"):
                raise ActionParseError("element clicks are plain left clicks; use mouse_click for other buttons")
            return {"name": "click_element", "element_id": str(arguments["bid"])}
        if name == "fill":
            return {"name": "fill", "element_id": str(arguments["bid"]), "text": arguments["value"]}
        if name == "clear":
            return {"name": "fill", "element_id": str(arguments["bid"]), "text": ""}
        if name == "select_option":
            return {"name": "select_option", "element_id": str(arguments["bid"]),
                    "option": single_option(arguments["options"])}
        if name == "scroll":
            return {"name": "scroll", "x": self.screen_width // 2, "y": self.screen_height // 2,
                    "delta_x": self.notches(arguments["delta_x"]), "delta_y": self.notches(arguments["delta_y"])}
        if name == "mouse_move":
            return {"name": "move", "x": pixel(arguments["x"]), "y": pixel(arguments["y"])}
        if name in ("mouse_click", "mouse_dblclick"):
            return {"name": "click", "x": pixel(arguments["x"]), "y": pixel(arguments["y"]),
                    "button": arguments.get("button", "left"), "count": 2 if name == "mouse_dblclick" else 1}
        if name == "mouse_drag_and_drop":
            return {"name": "drag", "path": [{"x": pixel(arguments["from_x"]), "y": pixel(arguments["from_y"])},
                                             {"x": pixel(arguments["to_x"]), "y": pixel(arguments["to_y"])}]}
        if name == "keyboard_press":
            return {"name": "key", "keys": arguments["key"]}
        if name in ("keyboard_type", "keyboard_insert_text"):
            return {"name": "type", "text": arguments["text"]}
        if name == "goto":
            return {"name": "goto", "url": arguments["url"]}
        if name == "go_back":
            return {"name": "back"}
        if name == "new_tab":
            return {"name": "tab", "operation": "new"}
        if name == "tab_close":
            return {"name": "tab", "operation": "close"}
        if name == "tab_focus":
            return {"name": "tab", "operation": "switch", "index": arguments["index"]}
        if name == "send_msg_to_user":
            return {"name": "answer", "text": arguments["text"]}
        if name == "report_infeasible":
            return {"name": "give_up", "reason": arguments["reason"]}
        return {"name": "wait", "duration_ms": arguments.get("wait_ms", DEFAULT_NOOP_MS)}

    def render(self, action: ActionModel) -> str:
        match action:
            case ElementClickAction():
                return f"click({action.element_id!r})"
            case FillAction():
                return f"fill({action.element_id!r}, {action.text!r})"
            case SelectOptionAction():
                return f"select_option({action.element_id!r}, {action.option!r})"
            case ScrollAction():
                return (f"scroll({action.delta_x * self.scroll_pixels_per_notch}, "
                        f"{action.delta_y * self.scroll_pixels_per_notch})")
            case MoveAction():
                return f"mouse_move({action.x}, {action.y})"
            case ClickAction() if action.count in (1, 2) and not action.modifiers:
                function_name = "mouse_click" if action.count == 1 else "mouse_dblclick"
                return f"{function_name}({action.x}, {action.y}, {action.button!r})"
            case DragAction() if len(action.path) == 2 and action.button == "left":
                start, end = action.path
                return f"mouse_drag_and_drop({start.x}, {start.y}, {end.x}, {end.y})"
            case KeyAction():
                return f"keyboard_press({render_playwright_combo(action.keys)!r})"
            case TypeAction():
                return f"keyboard_type({action.text!r})"
            case GotoAction():
                return f"goto({action.url!r})"
            case BackAction():
                return "go_back()"
            case TabAction() if action.operation == "new" and action.url is None:
                return "new_tab()"
            case TabAction() if action.operation == "close":
                return "tab_close()"
            case TabAction() if action.operation == "switch":
                return f"tab_focus({action.index})"
            case AnswerAction():
                return f"send_msg_to_user({action.text!r})"
            case GiveUpAction():
                return f"report_infeasible({action.reason!r})"
            case WaitAction():
                return f"noop({action.duration_ms})"
        raise ActionRenderError(f"BrowserGym cannot express {action.model_dump()}")


class DomSnapshotBuilder(HTMLParser):
    """HTML to the part of CDP's DOMSnapshot.captureSnapshot that BrowserGym's DOM flattening reads."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.strings: list[str] = []
        self.string_indexes: dict[str, int] = {}
        self.parent_index: list[int] = []
        self.node_type: list[int] = []
        self.node_name: list[int] = []
        self.node_value: list[int] = []
        self.attributes: list[list[int]] = []
        self.tags: list[str] = []
        self.open_elements: list[int] = []
        self.append_node(-1, DOCUMENT_NODE, "#document", None, [])

    def string(self, text: str | None) -> int:
        if text is None:
            return -1
        if text not in self.string_indexes:
            self.string_indexes[text] = len(self.strings)
            self.strings.append(text)
        return self.string_indexes[text]

    def append_node(self, parent: int, node_type: int, name: str, value: str | None, attributes: list[int]) -> int:
        self.parent_index.append(parent)
        self.node_type.append(node_type)
        self.node_name.append(self.string(name))
        self.node_value.append(self.string(value))
        self.attributes.append(attributes)
        self.tags.append(name.lower())
        return len(self.tags) - 1

    def add_node(self, node_type: int, name: str, value: str | None, attributes: list[int]) -> int:
        """A node under the innermost open element, or under the document."""
        parent = self.open_elements[-1] if self.open_elements else 0
        return self.append_node(parent, node_type, name, value, attributes)

    def element_attributes(self, attributes: list[tuple[str, str | None]]) -> list[int]:
        indexes = []
        for name, value in attributes:
            indexes += [self.string(BID_ATTRIBUTE if name == ELEMENT_ATTRIBUTE else name), self.string(value or "")]
        return indexes

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        index = self.add_node(ELEMENT_NODE, tag.upper(), None, self.element_attributes(attrs))
        if tag not in VOID_ELEMENTS:
            self.open_elements.append(index)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.add_node(ELEMENT_NODE, tag.upper(), None, self.element_attributes(attrs))

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.open_elements) - 1, -1, -1):
            if self.tags[self.open_elements[position]] == tag:
                del self.open_elements[position:]
                return

    def handle_data(self, data: str) -> None:
        self.add_node(TEXT_NODE, "#text", data, [])

    def handle_comment(self, data: str) -> None:
        self.add_node(COMMENT_NODE, "#comment", data, [])

    def snapshot(self) -> dict[str, Any]:
        nodes = {"parentIndex": self.parent_index, "nodeType": self.node_type, "nodeName": self.node_name,
                 "nodeValue": self.node_value, "attributes": self.attributes,
                 "contentDocumentIndex": {"index": [], "value": []}}
        return {"documents": [{"nodes": nodes}], "strings": self.strings}


def dom_snapshot(html: str) -> dict[str, Any]:
    builder = DomSnapshotBuilder()
    builder.feed(html)
    builder.close()
    return builder.snapshot()


def axtree_node(node: PrunedNode, child_ids: list[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "nodeId": node.node_id,
        "ignored": False,
        "role": {"type": "role", "value": node.role},
        "name": {"type": "computedString", "value": node.name},
        "properties": [{"name": state, "value": {"type": "booleanOrUndefined", "value": True}}
                       for state in node.states],
        "childIds": child_ids,
    }
    if node.parent_id is not None:
        entry["parentId"] = node.parent_id
    if node.value:
        entry["value"] = {"type": "string", "value": node.value}
    return entry


def axtree_object(nodes: Sequence[PrunedNode]) -> dict[str, Any]:
    """The pruned desktop tree in the shape of CDP's Accessibility.getFullAXTree, root first."""
    child_ids: dict[str, list[str]] = {}
    for node in nodes:
        if node.parent_id is not None:
            child_ids.setdefault(node.parent_id, []).append(node.node_id)
    return {"nodes": [axtree_node(node, child_ids.get(node.node_id, [])) for node in nodes]}


class FrameSource(Protocol):
    def frame(self, uri: str) -> bytes: ...


class ExperimentFrames:
    """Screenshots through experimentd's frame API, which the SDK reaches."""

    def __init__(self, client: ExperimentClient) -> None:
        self.client = client

    def frame(self, uri: str) -> bytes:
        return self.client.get_frame(uri.removeprefix(BLOB_SCHEME))


class BrowserGymEnv:
    """BrowserGym's reset and step over a ComputerUseEnv; the goal is the task's instruction."""

    def __init__(self, env: ComputerUseEnv, goal: str, actions: BrowserGymActions, frames: FrameSource | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.env = env
        self.goal = goal
        self.actions = actions
        self.frames = frames
        self.clock = clock
        self.started_at = clock()
        self.chat_messages: list[dict[str, Any]] = []
        self.last_action = ""
        self.last_action_error = ""

    def chat(self, role: str, message: str) -> None:
        self.chat_messages.append({"role": role, "timestamp": time.time(), "message": message})

    def screenshot(self, observation: Observation) -> Image.Image | None:
        if self.frames is None or observation.screenshot is None:
            return None
        return Image.open(io.BytesIO(self.frames.frame(observation.screenshot.uri))).convert("RGB")

    def observation(self, observation: Observation | dict[str, Any] | None) -> dict[str, Any]:
        parsed = observation if isinstance(observation, Observation) else Observation.model_validate(observation)
        return {
            "chat_messages": list(self.chat_messages),
            "goal": self.goal,
            "goal_object": [{"type": "text", "text": self.goal}],
            "open_pages_urls": [],
            "open_pages_titles": [],
            "active_page_index": 0,
            "url": "",
            "screenshot": self.screenshot(parsed),
            "dom_object": dom_snapshot(parsed.dom) if parsed.dom is not None else None,
            "axtree_object": axtree_object(parsed.accessibility) if parsed.accessibility is not None else None,
            "extra_element_properties": {},
            "focused_element_bid": "",
            "last_action": self.last_action,
            "last_action_error": self.last_action_error,
            "elapsed_time": self.clock() - self.started_at,
        }

    def reset(self) -> tuple[dict[str, Any], dict[str, Any]]:
        observation, info = self.env.reset()
        self.started_at = self.clock()
        self.chat_messages = []
        self.chat("user", self.goal)
        self.last_action, self.last_action_error = "", ""
        return self.observation(observation), info

    def step(self, action_text: str) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        """Plays the string's calls in order; a refused action leaves the env as it was and is reported."""
        self.last_action, self.last_action_error = action_text, ""
        try:
            actions = self.actions.parse(action_text)
        except ActionParseError as error:
            self.last_action_error = str(error)
            return self.observation(self.env.last_observation), 0.0, False, False, {}
        result: tuple[Any, float, bool, bool, dict[str, Any]] = (self.env.last_observation, 0.0, False, False, {})
        for action in actions:
            if isinstance(action, AnswerAction):
                self.chat("assistant", action.text)
            try:
                result = self.env.step(action)
            except ActionNotAllowedError as error:
                self.last_action_error = str(error)
                break
            if result[2] or result[3]:
                break
        observation, reward, terminated, truncated, info = result
        return self.observation(observation), reward, terminated, truncated, info

    def close(self) -> None:
        self.env.close()
