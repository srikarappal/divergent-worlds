"""Round trips, tool definitions and the adapter registry."""

import pytest

from rlworld.actions.adapters import AdapterCapabilityError, get_adapter, supported_modalities
from rlworld.actions.models import ClickAction, parse_action

FUNCTION_ACTIONS = [
    {"name": "goto", "url": "https://shop.example.com/cart"},
    {"name": "click_element", "element_id": "submit"},
    {"name": "fill", "element_id": "email", "text": "ops@example.com"},
    {"name": "select_option", "element_id": "quarter", "option": "Q3"},
    {"name": "back"},
    {"name": "tab", "operation": "new", "url": "https://mail.example.com"},
    {"name": "exec", "command": "cat /tmp/report.csv", "timeout_s": 20},
    {"name": "native_call", "app": "excel", "method": "range_write", "arguments": {"range": "A1", "value": 3}},
    {"name": "ask_user", "question": "Which quarter?"},
    {"name": "answer", "text": "42"},
    {"name": "done"},
    {"name": "give_up", "reason": "no access"},
]
ROUND_TRIP_ACTIONS = {
    ("computer_tool", (1280, 800)): [
        {"name": "click", "x": 10, "y": 20},
        {"name": "click", "x": 10, "y": 20, "button": "right"},
        {"name": "click", "x": 10, "y": 20, "button": "middle"},
        {"name": "click", "x": 10, "y": 20, "count": 2},
        {"name": "click", "x": 10, "y": 20, "count": 3},
        {"name": "click", "x": 10, "y": 20, "modifiers": ["ctrl", "shift"]},
        {"name": "move", "x": 1279, "y": 799},
        {"name": "drag", "path": [{"x": 1, "y": 2}, {"x": 300, "y": 400}]},
        {"name": "scroll", "x": 5, "y": 5, "delta_y": 4},
        {"name": "scroll", "x": 5, "y": 5, "delta_y": -2},
        {"name": "scroll", "x": 5, "y": 5, "delta_x": 3},
        {"name": "scroll", "x": 5, "y": 5, "delta_x": -1},
        {"name": "type", "text": "multi\nline 'quoted'"},
        {"name": "key", "keys": ["ctrl", "alt", "delete"]},
        {"name": "key", "keys": ["f5"]},
        {"name": "key", "keys": ["ctrl", "+"]},
        {"name": "wait", "duration_ms": 1500},
        {"name": "wait", "duration_ms": 0},
        *FUNCTION_ACTIONS,
    ],
    ("typed_actions", (1920, 1080)): [
        {"name": "click", "x": 10, "y": 20},
        {"name": "click", "x": 10, "y": 20, "button": "right"},
        {"name": "click", "x": 10, "y": 20, "button": "middle"},
        {"name": "click", "x": 10, "y": 20, "count": 2},
        {"name": "move", "x": 1919, "y": 1079},
        {"name": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}, {"x": 5, "y": 6}]},
        {"name": "scroll", "x": 5, "y": 5, "delta_x": -2, "delta_y": 7},
        {"name": "type", "text": "hello"},
        {"name": "key", "keys": ["ctrl", "page_up"]},
        {"name": "wait", "duration_ms": 1000},
        {"name": "wait", "duration_ms": 0},
        *FUNCTION_ACTIONS,
    ],
    ("text_actions", (1932, 1092)): [
        {"name": "click", "x": 10, "y": 20},
        {"name": "click", "x": 10, "y": 20, "button": "right"},
        {"name": "click", "x": 10, "y": 20, "count": 2},
        {"name": "move", "x": 1931, "y": 1091},
        {"name": "drag", "path": [{"x": 1, "y": 2}, {"x": 300, "y": 400}]},
        {"name": "scroll", "x": 5, "y": 5, "delta_y": 5},
        {"name": "scroll", "x": 5, "y": 5, "delta_x": -5},
        {"name": "type", "text": "it's\nfine \\o/"},
        {"name": "key", "keys": ["alt", "tab"]},
        {"name": "wait", "duration_ms": 5000},
        {"name": "answer", "text": "the answer"},
        {"name": "done"},
        {"name": "ask_user", "question": ""},
    ],
}


def round_trip_cases():
    cases = []
    for (format_name, env_size), payloads in ROUND_TRIP_ACTIONS.items():
        for payload in payloads:
            cases.append(pytest.param(format_name, env_size, payload, id=f"{format_name}: {payload['name']}"))
    return cases


@pytest.mark.parametrize(("format_name", "env_size", "payload"), round_trip_cases())
def test_render_then_parse_is_lossless_on_an_identity_screen(format_name, env_size, payload):
    adapter = get_adapter(format_name)
    geometry = adapter.model_geometry(*env_size)
    assert geometry.is_identity
    action = parse_action(payload)
    rendered = adapter.render_action(action, geometry)
    raw_output = rendered if isinstance(rendered, str) else [rendered]
    assert adapter.parse_output(raw_output, geometry) == [action]


def test_computer_tool_definitions():
    adapter = get_adapter("computer_tool")
    geometry = adapter.model_geometry(1920, 1080)
    definitions = adapter.tool_definitions(["computer", "control"], geometry)
    assert [definition["name"] for definition in definitions] == ["computer", "ask_user", "answer", "done", "give_up"]
    assert (definitions[0]["display_width_px"], definitions[0]["display_height_px"]) == (1280, 720)
    answer_schema = definitions[2]["input_schema"]
    assert answer_schema["required"] == ["text"]
    assert "name" not in answer_schema["properties"]


def test_typed_action_definitions():
    adapter = get_adapter("typed_actions", environment="windows")
    geometry = adapter.model_geometry(1920, 1080)
    definitions = adapter.tool_definitions(["computer", "browser", "control"], geometry)
    assert definitions[0] == {
        "type": "computer_use_preview",
        "display_width": 1920,
        "display_height": 1080,
        "environment": "windows",
    }
    function_names = [definition["name"] for definition in definitions[1:]]
    assert function_names[:6] == ["goto", "click_element", "fill", "select_option", "back", "tab"]
    assert "exec" not in function_names


def test_text_action_space_lists_only_allowed_modalities():
    adapter = get_adapter("text_actions")
    geometry = adapter.model_geometry(1920, 1080)
    computer_only = adapter.tool_definitions(["computer"], geometry)[0]
    with_control = adapter.tool_definitions(["computer", "control"], geometry)[0]
    assert (computer_only["screen_width"], computer_only["screen_height"]) == (1932, 1092)
    assert not any(line.startswith("finished") for line in computer_only["actions"])
    assert any(line.startswith("finished") for line in with_control["actions"])


QWEN_REPLY = """Thought: Open the applications menu to find a terminal.

Action: click(start_box='(38, 16)')

<tool_call>
<function=computer_use>
<parameter=action>
click
</parameter>
<parameter=start_box>
[38, 16]
</parameter>
</function>
</tool_call>"""


def test_qwen_native_tool_calls_never_double_or_break_text_actions():
    adapter = get_adapter("text_actions", coordinate_mode="relative_1000")
    geometry = adapter.model_geometry(1280, 800)
    expected = [ClickAction(x=49, y=13)]
    assert adapter.parse_output(QWEN_REPLY, geometry) == expected
    tool_call_only = QWEN_REPLY.split("Action:", 1)[1].split("\n", 2)[2]
    assert adapter.parse_output(tool_call_only, geometry) == expected


def test_text_actions_cannot_express_terminal_tasks():
    adapter = get_adapter("text_actions")
    with pytest.raises(AdapterCapabilityError) as raised:
        adapter.tool_definitions(["terminal", "control"], adapter.model_geometry(1920, 1080))
    assert "terminal" in str(raised.value)


def test_registry():
    assert supported_modalities("text_actions") == frozenset({"computer", "control"})
    assert get_adapter("computer_tool", max_width=1024, max_height=768).model_geometry(1920, 1080).model_width == 1024
    with pytest.raises(KeyError):
        get_adapter("smoke_signals")
