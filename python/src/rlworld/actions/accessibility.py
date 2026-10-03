"""Accessibility trees pruned to what is on screen (DESIGN.md section 6.3).

An Excel grid or a long page can expose thousands of nodes; models only need
the visible ones, so offscreen and invisible subtrees are dropped and the
result is capped.
"""

from pydantic import BaseModel, ConfigDict, NonNegativeInt

HIDDEN_STATES = frozenset({"invisible", "offscreen", "hidden"})


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bounds(FrozenModel):
    x: int
    y: int
    width: NonNegativeInt
    height: NonNegativeInt

    def intersects(self, other: "Bounds") -> bool:
        return (
            self.x < other.x + other.width
            and other.x < self.x + self.width
            and self.y < other.y + other.height
            and other.y < self.y + self.height
        )


class AccessibilityNode(FrozenModel):
    node_id: str
    role: str
    name: str = ""
    value: str = ""
    bounds: Bounds | None = None
    states: tuple[str, ...] = ()
    children: tuple["AccessibilityNode", ...] = ()


class PrunedNode(FrozenModel):
    node_id: str
    role: str
    name: str = ""
    value: str = ""
    bounds: Bounds | None = None
    states: tuple[str, ...] = ()
    depth: NonNegativeInt
    parent_id: str | None = None


AccessibilityNode.model_rebuild()


def is_visible(node: AccessibilityNode, viewport: Bounds) -> bool:
    if HIDDEN_STATES.intersection(node.states):
        return False
    if node.bounds is None:
        return True
    if node.bounds.width == 0 or node.bounds.height == 0:
        return False
    return node.bounds.intersects(viewport)


def prune_accessibility_tree(root: AccessibilityNode, viewport: Bounds, max_nodes: int = 400) -> list[PrunedNode]:
    """Depth first, document order; a hidden node drops its whole subtree."""
    pruned_nodes: list[PrunedNode] = []
    pending: list[tuple[AccessibilityNode, int, str | None]] = [(root, 0, None)]
    while pending and len(pruned_nodes) < max_nodes:
        node, depth, parent_id = pending.pop()
        if not is_visible(node, viewport):
            continue
        pruned_nodes.append(
            PrunedNode(
                node_id=node.node_id,
                role=node.role,
                name=node.name,
                value=node.value,
                bounds=node.bounds,
                states=node.states,
                depth=depth,
                parent_id=parent_id,
            )
        )
        for child in reversed(node.children):
            pending.append((child, depth + 1, node.node_id))
    return pruned_nodes
