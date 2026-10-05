"""AT-SPI accessibles to accessibility nodes (DESIGN.md section 6.3).

Works on anything shaped like a pyatspi Accessible, so the conversion is tested
without a desktop. Nodes are named by their path from the desktop, which stays
stable while the window layout does.
"""

from typing import Any, Protocol

from rlworld.actions.accessibility import AccessibilityNode, Bounds

SCREEN_COORDINATES = 0
KEPT_STATES = frozenset({"focused", "selected", "checked", "editable", "expanded", "pressed", "sensitive"})
# Spreadsheet tables report every cell as a child and mark themselves as managing their descendants.
MANAGES_DESCENDANTS = "manages-descendants"
MAX_CHILDREN = 500


class Accessible(Protocol):
    name: str
    childCount: int

    def getRoleName(self) -> str: ...

    def getState(self) -> Any: ...

    def getChildAtIndex(self, index: int) -> "Accessible | None": ...

    def queryComponent(self) -> Any: ...

    def queryText(self) -> Any: ...

    def queryValue(self) -> Any: ...


def node_value(accessible: Accessible) -> str:
    try:
        return accessible.queryText().getText(0, -1) or ""
    except NotImplementedError:
        pass
    try:
        return str(accessible.queryValue().currentValue)
    except NotImplementedError:
        return ""


def node_bounds(accessible: Accessible) -> Bounds | None:
    try:
        extents = accessible.queryComponent().getExtents(SCREEN_COORDINATES)
    except NotImplementedError:
        return None
    return Bounds(x=extents.x, y=extents.y, width=max(0, extents.width), height=max(0, extents.height))


def state_names(accessible: Accessible) -> set[str]:
    return {state.value_nick for state in accessible.getState().getStates()}


def node_states(names: set[str], on_screen_component: bool) -> tuple[str, ...]:
    """Kept states plus invisible for a component not both visible and showing, so pruning drops it.

    Application objects have no component and the desktop root, which has one since at-spi2-core 2.52, is never
    hidden this way.
    """
    states = sorted(names & KEPT_STATES)
    if on_screen_component and not {"visible", "showing"} <= names:
        states.append("invisible")
    return tuple(states)


def accessibility_node(accessible: Accessible, node_id: str = "0", depth: int = 0,
                       max_depth: int = 40) -> AccessibilityNode:
    """Children of nodes that manage their descendants stay unlisted and others stop at MAX_CHILDREN."""
    names = state_names(accessible)
    children = []
    if depth < max_depth and MANAGES_DESCENDANTS not in names:
        for index in range(min(accessible.childCount, MAX_CHILDREN)):
            child = accessible.getChildAtIndex(index)
            if child is not None:
                children.append(accessibility_node(child, f"{node_id}.{index}", depth + 1, max_depth))
    bounds = node_bounds(accessible)
    return AccessibilityNode(node_id=node_id, role=accessible.getRoleName(), name=accessible.name or "",
                             value=node_value(accessible), bounds=bounds,
                             states=node_states(names, bounds is not None and depth > 0), children=tuple(children))
