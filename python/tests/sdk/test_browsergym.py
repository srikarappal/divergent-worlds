import io

import pytest
from PIL import Image

from rlworld.actions.accessibility import Bounds, PrunedNode
from rlworld.actions.adapters import ActionParseError, ActionRenderError
from rlworld.actions.models import (
    AnswerAction,
    AskUserAction,
    BackAction,
    ClickAction,
    DoneAction,
    DragAction,
    ElementClickAction,
    ExecAction,
    FillAction,
    GiveUpAction,
    GotoAction,
    KeyAction,
    MoveAction,
    NativeCallAction,
    Observation,
    ScreenshotRef,
    ScrollAction,
    SelectOptionAction,
    TabAction,
    TypeAction,
    WaitAction,
    action_payload,
)
from rlworld.sdk import BrowserGymActions, BrowserGymEnv, ComputerUseEnv, ExperimentFrames, LeaseInfo, StepResult
from rlworld.sdk.browsergym import axtree_object, dom_snapshot

ACTIONS = BrowserGymActions(screen_width=1280, screen_height=800)
FRAME_DIGEST = "sha256:" + "ab" * 32
PAGE = ('<html data-rlworld-id="0"><head><title>CRM</title></head><body data-rlworld-id="1"><!-- nav -->'
        '<form data-rlworld-id="2"><input data-rlworld-id="3" name="q" disabled><br>'
        '<button data-rlworld-id="4">Go</button></form><p>a<b>bold</b>c</p></body></html>')
TREE = (PrunedNode(node_id="0", role="desktop frame", depth=0),
        PrunedNode(node_id="0.0", role="frame", name="CRM - Chrome", depth=1, parent_id="0",
                   bounds=Bounds(x=0, y=0, width=1280, height=800)),
        PrunedNode(node_id="0.0.1", role="entry", name="Search", value="acme", states=("focused", "editable"),
                   depth=2, parent_id="0.0"))
LEASE = LeaseInfo(lease_id="lease_00000001", env_id="env_00000001", host_id="host_a",
                  expires_at="2026-10-03T12:01:00Z")
BROWSERGYM_KEYS = {"chat_messages", "goal", "goal_object", "open_pages_urls", "open_pages_titles", "active_page_index",
                   "url", "screenshot", "dom_object", "axtree_object", "extra_element_properties",
                   "focused_element_bid", "last_action", "last_action_error", "elapsed_time"}


def parsed(text):
    return [action_payload(action) for action in ACTIONS.parse(text)]


@pytest.mark.parametrize("text, payload", [
    ("click('12')", {"name": "click_element", "element_id": "12"}),
    ("click(bid='a51')", {"name": "click_element", "element_id": "a51"}),
    ("click(7)", {"name": "click_element", "element_id": "7"}),
    ("click('12', button='left', modifiers=[])", {"name": "click_element", "element_id": "12"}),
    ("fill('7', 'ACME Corp')", {"name": "fill", "element_id": "7", "text": "ACME Corp"}),
    ("fill(bid='7', value='')", {"name": "fill", "element_id": "7", "text": ""}),
    ("clear('7')", {"name": "fill", "element_id": "7", "text": ""}),
    ("select_option('9', 'Overdue')", {"name": "select_option", "element_id": "9", "option": "Overdue"}),
    ("select_option('9', ['Overdue'])", {"name": "select_option", "element_id": "9", "option": "Overdue"}),
    ("goto('https://crm.example.com/deals')", {"name": "goto", "url": "https://crm.example.com/deals"}),
    ("go_back()", {"name": "back"}),
    ("new_tab()", {"name": "tab", "operation": "new", "index": None, "url": None}),
    ("tab_close()", {"name": "tab", "operation": "close", "index": None, "url": None}),
    ("tab_focus(2)", {"name": "tab", "operation": "switch", "index": 2, "url": None}),
    ("send_msg_to_user('The total is 1250')", {"name": "answer", "text": "The total is 1250"}),
    ("report_infeasible('no such report')", {"name": "give_up", "reason": "no such report"}),
    ("noop()", {"name": "wait", "duration_ms": 1000}),
    ("noop(wait_ms=250)", {"name": "wait", "duration_ms": 250}),
])
def test_bid_navigation_tab_and_chat_actions_become_canonical_actions(text, payload):
    assert parsed(text) == [payload]


@pytest.mark.parametrize("text, payload", [
    ("mouse_move(10.4, 20.6)", {"name": "move", "x": 10, "y": 21}),
    ("mouse_click(100, 200)", {"name": "click", "x": 100, "y": 200, "button": "left", "count": 1,
                               "modifiers": []}),
    ("mouse_click(100, 200, button='right')", {"name": "click", "x": 100, "y": 200, "button": "right", "count": 1,
                                               "modifiers": []}),
    ("mouse_dblclick(5, 6)", {"name": "click", "x": 5, "y": 6, "button": "left", "count": 2, "modifiers": []}),
    ("mouse_drag_and_drop(1, 2, 30, 40)", {"name": "drag", "path": [{"x": 1, "y": 2}, {"x": 30, "y": 40}],
                                           "button": "left"}),
    ("keyboard_press('Control+Shift+t')", {"name": "key", "keys": ["ctrl", "shift", "t"]}),
    ("keyboard_press('Enter')", {"name": "key", "keys": ["enter"]}),
    ("keyboard_press('ArrowDown')", {"name": "key", "keys": ["down"]}),
    ("keyboard_type('hello world')", {"name": "type", "text": "hello world"}),
    ("keyboard_insert_text('hi')", {"name": "type", "text": "hi"}),
    ("scroll(0, 200)", {"name": "scroll", "x": 640, "y": 400, "delta_x": 0, "delta_y": 2}),
    ("scroll(-30, 0)", {"name": "scroll", "x": 640, "y": 400, "delta_x": -1, "delta_y": 0}),
    ("scroll(0, -350.0)", {"name": "scroll", "x": 640, "y": 400, "delta_x": 0, "delta_y": -4}),
])
def test_coordinate_keyboard_and_scroll_actions_become_canonical_actions(text, payload):
    assert parsed(text) == [payload]


def test_several_calls_play_in_order_and_comments_are_ignored():
    text = "fill('3', 'Northwind')  # the customer\n# now submit\nclick('4')\n"
    assert parsed(text) == [{"name": "fill", "element_id": "3", "text": "Northwind"},
                            {"name": "click_element", "element_id": "4"}]


def test_scroll_notches_follow_the_configured_wheel_step():
    coarse = BrowserGymActions(screen_width=1920, screen_height=1080, scroll_pixels_per_notch=50)
    assert action_payload(coarse.parse("scroll(0, 200)")[0]) == {"name": "scroll", "x": 960, "y": 540,
                                                                  "delta_x": 0, "delta_y": 4}


@pytest.mark.parametrize("text, message", [
    ("", "no action found"),
    ("# just a comment", "no action found"),
    ("click(", "cannot parse"),
    ("page.click('a')", "plain function calls"),
    ("x = 1", "function calls, one per line"),
    ("hover('12')", "hover has no canonical action"),
    ("go_forward()", "go_forward has no canonical action"),
    ("upload_file('3', 'a.pdf')", "upload_file has no canonical action"),
    ("teleport('x')", "unknown BrowserGym action 'teleport'"),
    ("click(target)", "click takes literal arguments only"),
    ("click('1', 'left', [], 'extra')", "click takes at most 3 arguments"),
    ("fill('1', text='x')", "unexpected or repeated argument 'text'"),
    ("fill('1', 'a', value='b')", "unexpected or repeated argument 'value'"),
    ("click(**{'bid': '1'})", "unexpected or repeated argument None"),
    ("fill('1')", "fill is missing 'value'"),
    ("click('12', button='right')", "plain left clicks"),
    ("click('12', modifiers=['Control'])", "plain left clicks"),
    ("select_option('9', ['a', 'b'])", "exactly one option"),
    ("scroll(0, 0)", "invalid scroll action"),
    ("scroll('down', 1)", "'down' is not a scroll amount"),
    ("mouse_click('left', 2)", "'left' is not a coordinate"),
    ("mouse_click(-5, 2)", "invalid click action"),
    ("noop(90000)", "invalid wait action"),
    ("keyboard_press('Hyper+x')", "invalid key action"),
    ("tab_focus(-1)", "invalid tab action"),
    ("send_msg_to_user(42)", "invalid answer action"),
])
def test_action_strings_the_env_cannot_play_are_refused(text, message):
    with pytest.raises(ActionParseError) as error:
        ACTIONS.parse(text)
    assert message in str(error.value)


ROUND_TRIPS = [
    ElementClickAction(element_id="a51"),
    FillAction(element_id="7", text="it's \"quoted\"\nsecond line"),
    FillAction(element_id="7", text=""),
    SelectOptionAction(element_id="9", option="Overdue"),
    ScrollAction(x=640, y=400, delta_x=0, delta_y=3),
    ScrollAction(x=640, y=400, delta_x=-2, delta_y=1),
    MoveAction(x=3, y=4),
    ClickAction(x=10, y=20),
    ClickAction(x=10, y=20, button="middle"),
    ClickAction(x=10, y=20, button="right", count=2),
    DragAction(path=({"x": 1, "y": 2}, {"x": 3, "y": 4})),
    KeyAction(keys=("ctrl", "shift", "t")),
    KeyAction(keys=("ctrl", "+")),
    KeyAction(keys=("page_down",)),
    TypeAction(text="Grüße, 'world'"),
    GotoAction(url="https://crm.example.com/?q=a&b=c"),
    BackAction(),
    TabAction(operation="new"),
    TabAction(operation="close"),
    TabAction(operation="switch", index=3),
    AnswerAction(text="1250.00"),
    GiveUpAction(reason="the report is gone"),
    WaitAction(duration_ms=500),
]


@pytest.mark.parametrize("action", ROUND_TRIPS, ids=[action.name for action in ROUND_TRIPS])
def test_rendered_actions_parse_back_to_themselves(action):
    assert ACTIONS.parse(ACTIONS.render(action)) == [action]


def test_rendering_uses_browsergym_spellings():
    assert ACTIONS.render(FillAction(element_id="7", text="ACME")) == "fill('7', 'ACME')"
    assert ACTIONS.render(ScrollAction(x=1, y=1, delta_y=-2)) == "scroll(0, -200)"
    assert ACTIONS.render(ClickAction(x=1, y=2, count=2)) == "mouse_dblclick(1, 2, 'left')"
    assert ACTIONS.render(KeyAction(keys=("ctrl", "s"))) == "keyboard_press('Control+s')"
    assert ACTIONS.render(TabAction(operation="switch", index=1)) == "tab_focus(1)"


@pytest.mark.parametrize("action", [
    DoneAction(),
    AskUserAction(question="Which month?"),
    ExecAction(command="ls"),
    NativeCallAction(app="excel", method="write"),
    TabAction(operation="new", url="https://crm.example.com"),
    ClickAction(x=1, y=2, count=3),
    ClickAction(x=1, y=2, modifiers=("ctrl",)),
    DragAction(path=({"x": 1, "y": 2}, {"x": 3, "y": 4}, {"x": 5, "y": 6})),
    DragAction(path=({"x": 1, "y": 2}, {"x": 3, "y": 4}), button="right"),
])
def test_actions_without_a_browsergym_form_are_not_rendered(action):
    with pytest.raises(ActionRenderError):
        ACTIONS.render(action)


def flatten(snapshot, node_index=0):
    """A small version of BrowserGym's DOM flattening: tags with their bid first, text in place."""
    strings = snapshot["strings"]
    nodes = snapshot["documents"][0]["nodes"]
    children = [index for index, parent in enumerate(nodes["parentIndex"]) if parent == node_index]
    inner = "".join(flatten(snapshot, child) for child in children)
    node_type = nodes["nodeType"][node_index]
    if node_type == 3:
        return strings[nodes["nodeValue"][node_index]]
    if node_type != 1:
        return inner
    pairs = nodes["attributes"][node_index]
    attributes = "".join(f' {strings[pairs[at]]}="{strings[pairs[at + 1]]}"' for at in range(0, len(pairs), 2))
    tag = strings[nodes["nodeName"][node_index]].lower()
    return f"<{tag}{attributes}>{inner}</{tag}>"


def test_dom_snapshots_name_element_ids_bid_and_keep_the_tree():
    snapshot = dom_snapshot(PAGE)
    nodes = snapshot["documents"][0]["nodes"]
    assert len({len(nodes[key]) for key in ("parentIndex", "nodeType", "nodeName", "nodeValue", "attributes")}) == 1
    assert (nodes["parentIndex"][0], nodes["nodeType"][0], snapshot["strings"][nodes["nodeName"][0]]) == (
        -1, 9, "#document")
    assert "bid" in snapshot["strings"] and "data-rlworld-id" not in snapshot["strings"]
    assert 8 in nodes["nodeType"] and nodes["contentDocumentIndex"] == {"index": [], "value": []}
    assert flatten(snapshot) == (
        '<html bid="0"><head><title>CRM</title></head><body bid="1"><form bid="2">'
        '<input bid="3" name="q" disabled=""></input><br></br><button bid="4">Go</button></form>'
        '<p>a<b>bold</b>c</p></body></html>')


def test_dom_snapshots_survive_unclosed_and_self_closed_tags():
    assert flatten(dom_snapshot("<div><span>x</div><p>y")) == "<div><span>x</span></div><p>y</p>"
    assert flatten(dom_snapshot('<div><img src="a.png"/>after</div></p>')) == (
        '<div><img src="a.png"></img>after</div>')
    assert flatten(dom_snapshot("")) == ""


def test_the_desktop_tree_takes_the_shape_of_a_devtools_tree():
    tree = axtree_object(TREE)
    assert [node["nodeId"] for node in tree["nodes"]] == ["0", "0.0", "0.0.1"]
    assert tree["nodes"][0]["childIds"] == ["0.0"] and "parentId" not in tree["nodes"][0]
    entry = tree["nodes"][2]
    assert entry["parentId"] == "0.0" and entry["childIds"] == []
    assert (entry["role"], entry["name"], entry["value"]) == ({"type": "role", "value": "entry"},
                                                              {"type": "computedString", "value": "Search"},
                                                              {"type": "string", "value": "acme"})
    assert [prop["name"] for prop in entry["properties"]] == ["focused", "editable"]
    assert "value" not in tree["nodes"][1] and "browsergym_id" not in entry


def frame_bytes(size=(64, 40)):
    output = io.BytesIO()
    Image.new("RGB", size, "navy").save(output, format="WEBP")
    return output.getvalue()


def observation(step_index):
    return Observation(step_index=step_index, dom=PAGE, accessibility=TREE,
                       screenshot=ScreenshotRef(uri=f"blob:{FRAME_DIGEST}", width=64, height=40,
                                                sha256=FRAME_DIGEST))


class FakeEnvApi:
    def __init__(self):
        self.steps = []
        self.finished_with = None
        self.closed = False

    def reset(self, lease_id):
        return StepResult(observation=observation(0), env_seconds=0.5, settle_seconds=0.2, settle_capped=False)

    def step(self, lease_id, action):
        self.steps.append(action)
        return StepResult(observation=observation(len(self.steps)), env_seconds=0.4, settle_seconds=0.1,
                          settle_capped=False)

    def finish(self, lease_id, apps):
        self.finished_with = apps
        return {"files": {"answer.txt": {}}}

    def close(self, lease_id):
        self.closed = True


class FrameShelf:
    def __init__(self):
        self.requested = []

    def frame(self, uri):
        self.requested.append(uri)
        return frame_bytes()


class TickingClock:
    def __init__(self):
        self.now = 50.0

    def __call__(self):
        self.now += 1.5
        return self.now


def answered_reward(final_state, outcome):
    return 1.0 if outcome.answer_text == "1250" else 0.0


def browsergym_env(api, modalities=("browser", "control"), max_steps=5, frames=None):
    env = ComputerUseEnv(api, LEASE, modalities, max_steps, ["files"], reward_function=answered_reward)
    return BrowserGymEnv(env, "Find the open total.", ACTIONS, frames=frames, clock=TickingClock())


def test_reset_returns_browsergyms_observation_keys():
    frames = FrameShelf()
    observation_dict, info = browsergym_env(FakeEnvApi(), frames=frames).reset()
    assert set(observation_dict) == BROWSERGYM_KEYS and info == {"env_seconds": 0.5}
    assert observation_dict["goal"] == "Find the open total."
    assert observation_dict["goal_object"] == [{"type": "text", "text": "Find the open total."}]
    assert [(message["role"], message["message"]) for message in observation_dict["chat_messages"]] == [
        ("user", "Find the open total.")]
    assert observation_dict["screenshot"].size == (64, 40) and observation_dict["screenshot"].mode == "RGB"
    assert frames.requested == [f"blob:{FRAME_DIGEST}"]
    assert flatten(observation_dict["dom_object"]).startswith('<html bid="0">')
    assert observation_dict["axtree_object"]["nodes"][1]["name"]["value"] == "CRM - Chrome"
    assert (observation_dict["last_action"], observation_dict["last_action_error"]) == ("", "")
    assert (observation_dict["open_pages_urls"], observation_dict["active_page_index"]) == ([], 0)
    assert observation_dict["elapsed_time"] == 1.5


def test_steps_play_the_parsed_actions_in_order():
    api = FakeEnvApi()
    env = browsergym_env(api)
    env.reset()
    observation_dict, reward, terminated, truncated, info = env.step("fill('3', 'Northwind')\nclick('4')")
    assert api.steps == [FillAction(element_id="3", text="Northwind"), ElementClickAction(element_id="4")]
    assert (reward, terminated, truncated) == (0.0, False, False) and info["env_seconds"] == 0.4
    assert observation_dict["last_action"] == "fill('3', 'Northwind')\nclick('4')"
    assert observation_dict["last_action_error"] == "" and observation_dict["screenshot"] is None


def test_refused_actions_are_reported_and_leave_the_env_alone():
    api = FakeEnvApi()
    env = browsergym_env(api)
    env.reset()
    observation_dict, reward, terminated, truncated, info = env.step("hover('12')")
    assert api.steps == [] and (reward, terminated, truncated, info) == (0.0, False, False, {})
    assert observation_dict["last_action_error"] == "hover has no canonical action"
    observation_dict = env.step("mouse_click(1, 2)")[0]
    assert api.steps == [] and "outside the allowed modalities" in observation_dict["last_action_error"]
    assert env.step("click('4')")[0]["last_action_error"] == ""


def test_a_message_to_the_user_answers_and_ends_the_episode():
    api = FakeEnvApi()
    env = browsergym_env(api)
    env.reset()
    observation_dict, reward, terminated, truncated, info = env.step("send_msg_to_user('1250')\nclick('4')")
    assert (reward, terminated, truncated) == (1.0, True, False)
    assert api.finished_with == ["files"] and api.steps == []
    assert info["outcome"].final_action == "answer"
    assert observation_dict["chat_messages"][-1]["role"] == "assistant"
    assert observation_dict["chat_messages"][-1]["message"] == "1250"
    env.close()
    assert api.closed


def test_infeasible_reports_give_up_and_the_step_budget_truncates():
    api = FakeEnvApi()
    env = browsergym_env(api)
    env.reset()
    assert env.step("report_infeasible('no such report')")[1:4] == (0.0, True, False)
    budget = browsergym_env(FakeEnvApi(), max_steps=2)
    budget.reset()
    budget.step("click('1')")
    assert budget.step("click('2')")[1:4] == (0.0, False, True)


class FrameClient:
    def __init__(self):
        self.requested = []

    def get_frame(self, frame_digest, size="full"):
        self.requested.append((frame_digest, size))
        return b"frame"


def test_experiment_frames_fetch_screenshots_by_digest():
    client = FrameClient()
    assert ExperimentFrames(client).frame(f"blob:{FRAME_DIGEST}") == b"frame"
    assert client.requested == [(FRAME_DIGEST, "full")]
