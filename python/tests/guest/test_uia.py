import pytest

from rlworld.actions.accessibility import Bounds, prune_accessibility_tree
from rlworld.guest.agent import window_titled
from rlworld.guest.uia import (
    BOUNDING_RECTANGLE,
    CACHED_PROPERTIES,
    CONTROL_TYPE,
    EXPAND_COLLAPSE_STATE,
    HAS_KEYBOARD_FOCUS,
    IS_ENABLED,
    IS_OFFSCREEN,
    NAME,
    SELECTION_ITEM_IS_SELECTED,
    TOGGLE_STATE,
    VALUE_IS_READ_ONLY,
    VALUE_VALUE,
    uia_node,
)

VIEWPORT = Bounds(x=0, y=0, width=1280, height=800)
# UIA answers a property an element does not support with a sentinel object.
NOT_SUPPORTED = object()
PANE, WINDOW, BUTTON, EDIT, DATA_GRID, DATA_ITEM, CHECK_BOX, TREE_ITEM = (50033, 50032, 50000, 50004, 50028, 50029,
                                                                         50002, 50024)


class FakeElement:
    """A cached UIA element: properties by id and child elements."""

    def __init__(self, properties, children=()):
        self.properties = properties
        self.child_elements = list(children)

    def property_value(self, property_id):
        assert property_id in CACHED_PROPERTIES, f"property {property_id} is read without being cached"
        return self.properties.get(property_id, NOT_SUPPORTED)

    def children(self):
        return self.child_elements


def element(control_type, name="", rectangle=(0.0, 0.0, 100.0, 20.0), children=(), properties=None):
    return FakeElement({CONTROL_TYPE: control_type, NAME: name, BOUNDING_RECTANGLE: rectangle, IS_ENABLED: True,
                        IS_OFFSCREEN: False, **(properties or {})}, children)


@pytest.mark.parametrize(("control_type", "role"), [
    (WINDOW, "frame"), (BUTTON, "push button"), (EDIT, "entry"), (DATA_GRID, "table"), (DATA_ITEM, "table cell"),
    (CHECK_BOX, "check box"), (50020, "label"), (50030, "document frame"), (50999, "unknown"),
    (NOT_SUPPORTED, "unknown"),
])
def test_control_types_take_the_role_names_the_linux_agent_reports(control_type, role):
    desktop = element(PANE, children=(element(control_type),))
    node = uia_node(desktop)
    assert node.role == "desktop frame" and node.children[0].role == role


def test_states_come_only_from_properties_the_element_supports():
    cases = [
        ({HAS_KEYBOARD_FOCUS: True}, {"focused", "sensitive"}),
        ({IS_ENABLED: False}, set()),
        ({VALUE_IS_READ_ONLY: False}, {"editable", "sensitive"}),
        ({VALUE_IS_READ_ONLY: True}, {"sensitive"}),
        ({SELECTION_ITEM_IS_SELECTED: True}, {"selected", "sensitive"}),
        ({TOGGLE_STATE: 1}, {"checked", "sensitive"}),
        ({TOGGLE_STATE: 2}, {"sensitive"}),
        ({EXPAND_COLLAPSE_STATE: 1}, {"expanded", "sensitive"}),
        ({EXPAND_COLLAPSE_STATE: 2}, {"expanded", "sensitive"}),
        ({EXPAND_COLLAPSE_STATE: 3}, {"sensitive"}),
        ({IS_OFFSCREEN: True}, {"offscreen", "sensitive"}),
    ]
    for properties, states in cases:
        node = uia_node(element(PANE, children=(element(TREE_ITEM, properties=properties),))).children[0]
        assert set(node.states) == states, properties
        assert list(node.states) == sorted(node.states)
    unsupported = {property_id: NOT_SUPPORTED for property_id in CACHED_PROPERTIES}
    node = uia_node(FakeElement({}, children=(FakeElement(unsupported),))).children[0]
    assert (node.role, node.name, node.value, node.bounds, node.states) == ("unknown", "", "", None, ())


def test_rectangles_names_and_values():
    cell = element(DATA_ITEM, name="B2", rectangle=(600.4, 380.6, 80.2, -3.0),
                   properties={VALUE_VALUE: "1250", VALUE_IS_READ_ONLY: False})
    node = uia_node(element(PANE, rectangle=(0.0, 0.0, 1280.0, 800.0), children=(cell,))).children[0]
    assert node.bounds == Bounds(x=600, y=381, width=80, height=0)
    assert (node.name, node.value) == ("B2", "1250")
    for rectangle in (NOT_SUPPORTED, (1.0, 2.0, 3.0), None):
        assert uia_node(element(BUTTON, rectangle=rectangle)).bounds is None


def excel_window():
    visible = element(DATA_ITEM, name="A1", rectangle=(40.0, 120.0, 64.0, 20.0), properties={VALUE_VALUE: "open"})
    scrolled_away = element(DATA_ITEM, name="A900", rectangle=(40.0, 18000.0, 64.0, 20.0),
                            properties={IS_OFFSCREEN: True})
    grid = element(DATA_GRID, name="Grid", rectangle=(0.0, 100.0, 1280.0, 680.0), children=(visible, scrolled_away))
    window = element(WINDOW, name="reconciliation.xlsx - Excel", rectangle=(0.0, 0.0, 1280.0, 800.0),
                     children=(grid,))
    return element(PANE, name="Desktop 1", rectangle=(0.0, 0.0, 1280.0, 800.0), children=(window,))


def test_offscreen_cells_are_pruned_and_setup_finds_the_window():
    tree = uia_node(excel_window())
    pruned = prune_accessibility_tree(tree, VIEWPORT)
    assert [(node.node_id, node.role, node.name) for node in pruned] == [
        ("0", "desktop frame", "Desktop 1"), ("0.0", "frame", "reconciliation.xlsx - Excel"),
        ("0.0.0", "table", "Grid"), ("0.0.0.0", "table cell", "A1")]
    assert pruned[3].parent_id == "0.0.0" and pruned[3].value == "open"
    assert window_titled(tree, "reconciliation.xlsx") and not window_titled(tree, "Grid")


def test_node_ids_follow_the_path_and_depth_is_capped():
    leaf = element(BUTTON, name="OK")
    dialog = element(WINDOW, name="Save As", children=(element(EDIT), leaf))
    tree = uia_node(element(PANE, children=(dialog,)))
    assert [child.node_id for child in tree.children[0].children] == ["0.0.0", "0.0.1"]
    shallow = uia_node(element(PANE, children=(dialog,)), max_depth=1)
    assert shallow.children[0].name == "Save As" and shallow.children[0].children == ()
