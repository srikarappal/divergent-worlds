"""UI Automation elements to accessibility nodes (DESIGN.md section 6.3).

The Windows helper reads the session's UIA tree; this module turns anything
shaped like a cached UIA element into the nodes the Linux guest agent builds
from AT-SPI, so the conversion is tested without Windows. Control types take
AT-SPI role names, so policies see one role vocabulary on both OS families and
task setup finds Windows windows as frames. UIA answers a property an element
does not support with a sentinel object, so only values of the expected type
count. Nodes are named by their path from the desktop.
"""

from collections.abc import Sequence
from typing import Any, Protocol

from rlworld.actions.accessibility import AccessibilityNode, Bounds

# UIA property ids the helper caches for every element.
BOUNDING_RECTANGLE = 30001
CONTROL_TYPE = 30003
NAME = 30005
HAS_KEYBOARD_FOCUS = 30008
IS_ENABLED = 30010
IS_OFFSCREEN = 30022
VALUE_VALUE = 30045
VALUE_IS_READ_ONLY = 30046
EXPAND_COLLAPSE_STATE = 30070
SELECTION_ITEM_IS_SELECTED = 30079
TOGGLE_STATE = 30086
CACHED_PROPERTIES = (BOUNDING_RECTANGLE, CONTROL_TYPE, NAME, HAS_KEYBOARD_FOCUS, IS_ENABLED, IS_OFFSCREEN,
                     VALUE_VALUE, VALUE_IS_READ_ONLY, EXPAND_COLLAPSE_STATE, SELECTION_ITEM_IS_SELECTED, TOGGLE_STATE)
TOGGLE_ON = 1
EXPANDED_STATES = frozenset({1, 2})
DESKTOP_ROLE = "desktop frame"
UNKNOWN_ROLE = "unknown"
# UIA control type ids to the AT-SPI role names the Linux guest agent reports.
CONTROL_TYPE_ROLES = {
    50000: "push button",
    50001: "calendar",
    50002: "check box",
    50003: "combo box",
    50004: "entry",
    50005: "link",
    50006: "image",
    50007: "list item",
    50008: "list",
    50009: "menu",
    50010: "menu bar",
    50011: "menu item",
    50012: "progress bar",
    50013: "radio button",
    50014: "scroll bar",
    50015: "slider",
    50016: "spin button",
    50017: "status bar",
    50018: "page tab list",
    50019: "page tab",
    50020: "label",
    50021: "tool bar",
    50022: "tool tip",
    50023: "tree",
    50024: "tree item",
    50026: "panel",
    50028: "table",
    50029: "table cell",
    50030: "document frame",
    50031: "push button",
    50032: "frame",
    50033: "panel",
    50034: "header",
    50035: "table column header",
    50036: "table",
    50037: "panel",
    50038: "separator",
    50039: "panel",
    50040: "tool bar",
}


class UiaElement(Protocol):
    def property_value(self, property_id: int) -> Any: ...

    def children(self) -> Sequence["UiaElement"]: ...


def node_bounds(rectangle: Any) -> Bounds | None:
    """UIA reports a rectangle as left, top, width and height in screen pixels."""
    if not isinstance(rectangle, (tuple, list)) or len(rectangle) != 4:
        return None
    left, top, width, height = (round(part) for part in rectangle)
    return Bounds(x=left, y=top, width=max(0, width), height=max(0, height))


def node_states(element: UiaElement) -> tuple[str, ...]:
    """The states the Linux agent keeps, plus offscreen for an element out of view, so pruning drops it."""
    states = set()
    if element.property_value(HAS_KEYBOARD_FOCUS) is True:
        states.add("focused")
    if element.property_value(IS_ENABLED) is True:
        states.add("sensitive")
    if element.property_value(VALUE_IS_READ_ONLY) is False:
        states.add("editable")
    if element.property_value(SELECTION_ITEM_IS_SELECTED) is True:
        states.add("selected")
    toggle = element.property_value(TOGGLE_STATE)
    if isinstance(toggle, int) and toggle == TOGGLE_ON:
        states.add("checked")
    expansion = element.property_value(EXPAND_COLLAPSE_STATE)
    if isinstance(expansion, int) and expansion in EXPANDED_STATES:
        states.add("expanded")
    if element.property_value(IS_OFFSCREEN) is True:
        states.add("offscreen")
    return tuple(sorted(states))


def text_property(element: UiaElement, property_id: int) -> str:
    value = element.property_value(property_id)
    return value if isinstance(value, str) else ""


def node_role(element: UiaElement, depth: int) -> str:
    if depth == 0:
        return DESKTOP_ROLE
    control_type = element.property_value(CONTROL_TYPE)
    return CONTROL_TYPE_ROLES.get(control_type, UNKNOWN_ROLE) if isinstance(control_type, int) else UNKNOWN_ROLE


def uia_node(element: UiaElement, node_id: str = "0", depth: int = 0, max_depth: int = 40) -> AccessibilityNode:
    children = []
    if depth < max_depth:
        children = [uia_node(child, f"{node_id}.{index}", depth + 1, max_depth)
                    for index, child in enumerate(element.children())]
    return AccessibilityNode(node_id=node_id, role=node_role(element, depth), name=text_property(element, NAME),
                             value=text_property(element, VALUE_VALUE),
                             bounds=node_bounds(element.property_value(BOUNDING_RECTANGLE)),
                             states=node_states(element), children=tuple(children))
