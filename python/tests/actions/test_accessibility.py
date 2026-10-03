from rlworld.actions.accessibility import AccessibilityNode, Bounds, prune_accessibility_tree

VIEWPORT = Bounds(x=0, y=0, width=1280, height=800)


def node(node_id: str, bounds: tuple[int, int, int, int] | None, *children, states=()) -> AccessibilityNode:
    node_bounds = Bounds(x=bounds[0], y=bounds[1], width=bounds[2], height=bounds[3]) if bounds else None
    return AccessibilityNode(node_id=node_id, role="cell", name=node_id, bounds=node_bounds, states=states,
                             children=tuple(children))


def test_offscreen_and_hidden_subtrees_are_dropped():
    root = node(
        "window",
        None,
        node("visible_cell", (10, 10, 50, 20)),
        node("offscreen_cell", (10, 5000, 50, 20), node("child_of_offscreen", (10, 10, 5, 5))),
        node("hidden_panel", (0, 0, 100, 100), node("child_of_hidden", (1, 1, 5, 5)), states=("invisible",)),
        node("zero_size", (5, 5, 0, 10)),
    )
    pruned_ids = [pruned.node_id for pruned in prune_accessibility_tree(root, VIEWPORT)]
    assert pruned_ids == ["window", "visible_cell"]


def test_document_order_depth_and_parents_are_kept():
    root = node("window", None, node("toolbar", (0, 0, 1280, 40), node("save", (0, 0, 20, 20))),
                node("grid", (0, 40, 1280, 700)))
    pruned = prune_accessibility_tree(root, VIEWPORT)
    assert [(item.node_id, item.depth, item.parent_id) for item in pruned] == [
        ("window", 0, None),
        ("toolbar", 1, "window"),
        ("save", 2, "toolbar"),
        ("grid", 1, "window"),
    ]


def test_large_grids_are_capped():
    cells = [node(f"cell_{index}", (index % 64 * 20, index // 64 * 20, 20, 20)) for index in range(5000)]
    pruned = prune_accessibility_tree(node("grid", None, *cells), VIEWPORT, max_nodes=100)
    assert len(pruned) == 100
    assert pruned[0].node_id == "grid"
