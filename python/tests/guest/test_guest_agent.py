import base64
import io
import json
import os
import socket
import struct
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from PIL import Image

from rlworld.actions.accessibility import AccessibilityNode, Bounds
from rlworld.actions.models import TerminalOutput, parse_action
from rlworld.gateway import BlobImageResolver
from rlworld.guest import GuestAgent, GuestFailure, VsockServer, serve_stream, xdotool_argv
from rlworld.guest.accessibility_tree import accessibility_node
from rlworld.guest.agent import file_snapshot
from rlworld.guest.cdp import CdpBrowser
from rlworld.guest.system import entropy_payload, user_command_argv
from rlworld.trajectories import InMemoryObjectStore
from rlworld.trajectories.object_store import put_blob
from rlworld.worlds.generator import generate_mid_market_company

VIEWPORT = Bounds(x=0, y=0, width=1280, height=800)


class FakeScreen:
    def __init__(self, color="white"):
        self.color = color

    def capture(self):
        return Image.new("RGB", (1280, 800), self.color)


class RecordingRunner:
    def __init__(self):
        self.commands = []

    def run(self, argv):
        self.commands.append(argv)


class FakeAccessibility:
    def tree(self):
        hidden = AccessibilityNode(node_id="0.0.1", role="push button", name="Hidden", states=("invisible",),
                                   bounds=Bounds(x=10, y=10, width=50, height=20))
        cell = AccessibilityNode(node_id="0.0.0", role="table cell", name="B2", value="1250",
                                 bounds=Bounds(x=600, y=380, width=80, height=40))
        window = AccessibilityNode(node_id="0.0", role="frame", name="Excel",
                                   bounds=Bounds(x=0, y=0, width=1280, height=800), children=(cell, hidden))
        return AccessibilityNode(node_id="0", role="desktop frame", children=(window,))


class FakeBrowser:
    def __init__(self, busy=False):
        self.is_busy = busy
        self.performed = []
        self.cookies = []

    def dom(self):
        return "<html><body data-rlworld-id='0'></body></html>"

    def busy(self):
        return self.is_busy

    def perform(self, action):
        self.performed.append(action.name)

    def set_cookie(self, name, value, domain):
        self.cookies.append((name, value, domain))


class FakeShell:
    def __init__(self):
        self.launched = []

    def execute(self, command, timeout_s):
        return TerminalOutput(stdout="x" * 50, stderr="", exit_code=0 if command == "ls" else 2)

    def launch(self, command):
        self.launched.append(command)


class FakeHygiene:
    def __init__(self):
        self.clock = []
        self.entropy = []

    def set_clock(self, when):
        self.clock.append(when)

    def add_entropy(self, entropy):
        self.entropy.append(entropy)


class RecordingSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


@pytest.fixture
def agent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return GuestAgent(
        screen=FakeScreen(), input_runner=RecordingRunner(), accessibility=FakeAccessibility(), browser=FakeBrowser(),
        shell=FakeShell(), hygiene=FakeHygiene(), home=home, world_root=tmp_path / "world",
        license_root=tmp_path / "licenses", secrets_path=tmp_path / "run" / "episode.env",
        launchers={"Excel": "libreoffice --calc"}, viewport=VIEWPORT, max_output_characters=20, sleep=RecordingSleep(),
    )


def action(payload):
    return parse_action(payload)


def test_computer_actions_become_xdotool_commands():
    assert xdotool_argv(action({"name": "click", "x": 10, "y": 20, "count": 2, "modifiers": ["ctrl", "shift"]}), 12) \
        == ["xdotool", "keydown", "ctrl", "keydown", "shift", "mousemove", "--sync", "10", "20", "click", "--repeat",
            "2", "1", "keyup", "shift", "keyup", "ctrl"]
    assert xdotool_argv(action({"name": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}, {"x": 5, "y": 6}],
                                "button": "right"}), 12) == [
        "xdotool", "mousemove", "--sync", "1", "2", "mousedown", "3", "mousemove", "--sync", "3", "4",
        "mousemove", "--sync", "5", "6", "mouseup", "3"]
    assert xdotool_argv(action({"name": "scroll", "x": 5, "y": 6, "delta_y": -3, "delta_x": 2}), 12) == [
        "xdotool", "mousemove", "--sync", "5", "6", "click", "--repeat", "3", "4", "click", "--repeat", "2", "7"]
    assert xdotool_argv(action({"name": "type", "text": "-1250"}), 12) == [
        "xdotool", "type", "--delay", "12", "--", "-1250"]
    assert xdotool_argv(action({"name": "key", "keys": ["ctrl", "s"]}), 12) == ["xdotool", "key", "--", "ctrl+s"]
    assert xdotool_argv(action({"name": "key", "keys": ["meta", "enter"]}), 12)[-1] == "super+Return"


def test_actions_go_to_their_backend(agent):
    agent.handle("act", {"action": {"name": "move", "x": 1, "y": 2}})
    agent.handle("act", {"action": {"name": "wait", "duration_ms": 250}})
    agent.handle("act", {"action": {"name": "goto", "url": "https://crm.example.com"}})
    agent.handle("act", {"action": {"name": "exec", "command": "ls"}})
    assert agent.input_runner.commands == [["xdotool", "mousemove", "--sync", "1", "2"]]
    assert agent.sleep.calls == [0.25] and agent.browser.performed == ["goto"]
    terminal = agent.handle("observe", {"kinds": ["screenshot"]})["terminal"]
    assert terminal["exit_code"] == 0 and terminal["stdout"].startswith("x" * 20 + "\n[truncated 30")
    assert "terminal" not in agent.handle("observe", {"kinds": ["screenshot"]})
    for unsupported in ({"name": "native_call", "app": "excel", "method": "write"}, {"name": "done"}):
        with pytest.raises(GuestFailure) as failure:
            agent.handle("act", {"action": unsupported})
        assert failure.value.code == "unsupported_action"


def test_screen_hashes_settle_unless_pages_are_still_loading(agent):
    first = agent.handle("screen_hash", {})
    assert agent.handle("screen_hash", {}) == first
    agent.screen.color = "black"
    assert agent.handle("screen_hash", {}) != first
    agent.browser.is_busy = True
    assert agent.handle("screen_hash", {}) != agent.handle("screen_hash", {})


def test_observations_carry_a_webp_frame_and_the_pruned_tree(agent):
    observation = agent.handle("observe", {"kinds": ["screenshot", "accessibility_tree", "dom"]})
    frame = Image.open(io.BytesIO(base64.b64decode(observation["screenshot_data"])))
    assert frame.format == "WEBP" and observation["screenshot_size"] == [1280, 800]
    assert [node["name"] for node in observation["accessibility"]] == ["", "Excel", "B2"]
    assert observation["accessibility"][2]["value"] == "1250" and observation["dom"].startswith("<html>")
    assert "dom" not in agent.handle("observe", {"kinds": ["screenshot"]})


def test_prepare_sets_the_world_clock_and_credits_entropy(agent):
    agent.handle("prepare", {"now": "2026-03-14T09:00:05+00:00", "entropy": base64.b64encode(b"fresh").decode()})
    agent.handle("prepare", {"now": "2026-03-14T09:00:09+00:00"})
    assert agent.hygiene.clock == [datetime(2026, 3, 14, 9, 0, 5, tzinfo=timezone.utc),
                                   datetime(2026, 3, 14, 9, 0, 9, tzinfo=timezone.utc)]
    assert agent.hygiene.entropy == [b"fresh"]


def test_injection_writes_world_setup_and_entitlements_inside_their_roots(agent):
    world = generate_mid_market_company(seed=7, size={"employees": 3, "customers": 2, "emails_per_person": 2})
    agent.handle("inject", {
        "world": world.model_dump(mode="json"), "apps": ["mail", "files"],
        "files": {"Documents/invoices.csv": {"text": "invoice,amount\n"},
                  "Documents/logo.png": {"base64": base64.b64encode(b"\x89PNG").decode()}},
        "license_tokens": {"office_ltsc": "token_abc"},
        "environment": {"CRM_API_KEY": "crm_secret"},
        "browser_cookies": {"session_crm.example": "cookie_123"},
        "open_windows": ["Excel"],
    })
    assert (agent.home / "Documents" / "invoices.csv").read_text() == "invoice,amount\n"
    assert (agent.license_root / "office_ltsc").read_text() == "token_abc"
    assert agent.secrets_path.read_text() == "CRM_API_KEY=crm_secret\n"
    assert agent.secrets_path.stat().st_mode & 0o777 == 0o600
    assert agent.browser.cookies == [("session_crm.example", "cookie_123", "crm.example")]
    assert agent.shell.launched == ["libreoffice --calc"]
    assert agent.handle("observe", {"kinds": ["file_diff"]})["file_changes"] == []
    (agent.home / "Documents" / "invoices.csv").write_text("invoice,amount\nINV1,1250\n")
    (agent.home / "Documents" / "summary.txt").write_text("done")
    assert agent.handle("observe", {"kinds": ["file_diff"]})["file_changes"] == [
        {"path": "Documents/invoices.csv", "change": "modified"},
        {"path": "Documents/summary.txt", "change": "created"}]
    extracted = agent.handle("extract", {"apps": ["mail", "files"]})
    assert set(extracted) == {"mail", "files"} and extracted["mail"]["inbox"]


class RecordingChown:
    def __init__(self):
        self.calls = []

    def __call__(self, path, uid, gid):
        self.calls.append((Path(path).name, uid, gid))


def test_the_materialized_world_belongs_to_the_desktop_user(tmp_path, monkeypatch):
    chown = RecordingChown()
    monkeypatch.setattr(os, "chown", chown)
    home = tmp_path / "home"
    home.mkdir()
    owned = GuestAgent(
        screen=FakeScreen(), input_runner=RecordingRunner(), accessibility=FakeAccessibility(), browser=FakeBrowser(),
        shell=FakeShell(), hygiene=FakeHygiene(), home=home, world_root=home / "world",
        license_root=tmp_path / "licenses", secrets_path=tmp_path / "run" / "episode.env", launchers={},
        viewport=VIEWPORT, owner=(1000, 1000))
    world = generate_mid_market_company(seed=7, size={"employees": 3, "customers": 2, "emails_per_person": 2})
    owned.handle("inject", {"world": world.model_dump(mode="json"), "apps": ["files"]})
    assert {"world", "files", "invoices.csv", "customers.csv"} <= {name for name, _, _ in chown.calls}
    assert {(uid, gid) for _, uid, gid in chown.calls} == {(1000, 1000)}


@pytest.mark.parametrize("payload, code", [
    ({"files": {"../../etc/cron.d/escape": {"text": "* * * * * root id"}}}, "bad_request"),
    ({"files": {"/etc/passwd": {"text": "root::0:0"}}}, "bad_request"),
    ({"open_windows": ["Photoshop"]}, "bad_request"),
    ({"browser_cookies": {"tracking": "x"}}, "bad_request"),
])
def test_injection_refuses_escapes_and_unknown_requests(agent, payload, code):
    with pytest.raises(GuestFailure) as failure:
        agent.handle("inject", payload)
    assert failure.value.code == code


class ExplodingHandler:
    def handle(self, method, params):
        if method == "boom":
            raise RuntimeError("backend crashed")
        if method == "refuse":
            raise GuestFailure("unsupported_action", "not here")
        return {"echo": params}


def exchange(lines):
    host, guest = socket.socketpair()
    with host, guest:
        server_thread = threading.Thread(target=serve_guest_side, args=(guest,))
        server_thread.start()
        host.sendall(b"".join(lines))
        host.shutdown(socket.SHUT_WR)
        replies = host.makefile("rb").read().splitlines()
        server_thread.join(5)
    return [json.loads(reply) for reply in replies]


def serve_guest_side(connection):
    with connection, connection.makefile("rwb") as stream:
        serve_stream(stream, ExplodingHandler(), threading.Lock())


def test_the_stream_server_answers_every_request_and_survives_backend_failures():
    replies = exchange([b'{"id": 1, "method": "echo", "params": {"a": 1}}\n', b'{"id": 2, "method": "boom"}\n',
                        b'{"id": 3, "method": "refuse"}\n', b"not json\n"])
    assert replies[0] == {"id": 1, "result": {"echo": {"a": 1}}}
    assert replies[1]["error"] == {"code": "internal", "message": "RuntimeError: backend crashed"}
    assert replies[2]["error"]["code"] == "unsupported_action"
    assert replies[3]["error"]["code"] == "bad_request" and replies[3]["id"] is None


def test_only_the_host_may_connect():
    server = VsockServer(ExplodingHandler())
    assert server.accepts(2) and not server.accepts(3) and not server.accepts(1)


class FakeState:
    def __init__(self, nicks):
        self.nicks = nicks

    def getStates(self):
        return [StateNick(nick) for nick in self.nicks]


class StateNick:
    def __init__(self, nick):
        self.value_nick = nick


class FakeExtents:
    def __init__(self, x, y, width, height):
        self.x, self.y, self.width, self.height = x, y, width, height


class FakeComponent:
    def __init__(self, extents):
        self.extents = extents

    def getExtents(self, coordinate_type):
        return self.extents


class FakeText:
    def __init__(self, text):
        self.text = text

    def getText(self, start, end):
        return self.text


class FakeAccessible:
    def __init__(self, role, name="", states=(), extents=None, text=None, children=()):
        self.role = role
        self.name = name
        self.states = states
        self.extents = extents
        self.text = text
        self.children = list(children)
        self.childCount = len(self.children)

    def getRoleName(self):
        return self.role

    def getState(self):
        return FakeState(self.states)

    def getChildAtIndex(self, index):
        return self.children[index]

    def queryComponent(self):
        if self.extents is None:
            raise NotImplementedError
        return FakeComponent(self.extents)

    def queryText(self):
        if self.text is None:
            raise NotImplementedError
        return FakeText(self.text)

    def queryValue(self):
        raise NotImplementedError


def test_atspi_accessibles_become_nodes_and_hidden_components_are_marked():
    field = FakeAccessible("text", "Total", ("visible", "showing", "editable", "focused"),
                           FakeExtents(10, 20, 100, 30), text="1250")
    hidden = FakeAccessible("push button", "Advanced", ("visible",), FakeExtents(0, 0, 10, 10))
    window = FakeAccessible("frame", "Invoices", ("visible", "showing"), FakeExtents(0, 0, 1280, 800),
                            children=(field, hidden))
    desktop = FakeAccessible("desktop frame", children=(FakeAccessible("application", "soffice", (), None,
                                                                       children=(window,)),))
    root = accessibility_node(desktop)
    window_node = root.children[0].children[0]
    assert (root.states, root.children[0].states) == ((), ())
    assert window_node.children[0] == AccessibilityNode(
        node_id="0.0.0.0", role="text", name="Total", value="1250", bounds=Bounds(x=10, y=20, width=100, height=30),
        states=("editable", "focused"))
    assert window_node.children[1].states == ("invisible",)
    desktop_with_component = FakeAccessible("desktop frame", "main", (), FakeExtents(0, 0, 1024, 768),
                                            children=(window,))
    assert accessibility_node(desktop_with_component).states == ()


def test_tables_that_manage_their_descendants_keep_their_cells_unlisted_and_long_child_lists_stop():
    cells = tuple(FakeAccessible("table cell", f"A{index}", ("visible", "showing"), FakeExtents(0, index, 10, 1))
                  for index in range(600))
    sheet = FakeAccessible("table", "Sheet1", ("visible", "showing", "manages-descendants"),
                           FakeExtents(0, 0, 1280, 700), children=cells)
    panel = FakeAccessible("panel", "Cells", ("visible", "showing"), FakeExtents(0, 0, 1280, 700), children=cells)
    window = FakeAccessible("frame", "Calc", ("visible", "showing"), FakeExtents(0, 0, 1280, 800),
                            children=(sheet, panel))
    window_node = accessibility_node(window)
    assert window_node.children[0].children == () and len(window_node.children[1].children) == 500


class FakeSocket:
    """A DevTools page socket: replies to each command, after an event that must be skipped."""

    def __init__(self, url, log):
        self.url = url
        self.log = log
        self.replies = []

    def send(self, payload):
        message = json.loads(payload)
        self.log.append((self.url, message["method"], message["params"]))
        result = {"result": {"value": "<html data-rlworld-id=\"0\"></html>"}}
        if message["method"] == "Target.createTarget":
            result = {"targetId": "page_2"}
        if "BUSY" in json.dumps(message["params"]) or "readyState" in json.dumps(message["params"]):
            result = {"result": {"value": True}}
        self.replies.append(json.dumps({"method": "Network.requestWillBeSent", "params": {}}))
        self.replies.append(json.dumps({"id": message["id"], "result": result}))

    def recv(self):
        return self.replies.pop(0)

    def close(self):
        self.log.append((self.url, "close", {}))


class FakeDevTools:
    def __init__(self, pages):
        self.pages = pages
        self.log = []

    def connect(self, url):
        return FakeSocket(url, self.log)

    def targets(self, request):
        return httpx.Response(200, json=self.pages)


def devtools_browser(pages):
    devtools = FakeDevTools(pages)
    client = httpx.Client(transport=httpx.MockTransport(devtools.targets))
    return CdpBrowser("http://127.0.0.1:9222", client, devtools.connect), devtools


def page(target_id):
    return {"id": target_id, "type": "page", "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{target_id}"}


def test_devtools_drives_pages_by_observed_element_ids():
    browser, devtools = devtools_browser([page("page_1"), {"id": "worker", "type": "service_worker"}])
    assert browser.dom().startswith("<html data-rlworld-id")
    browser.perform(action({"name": "fill", "element_id": "17", "text": "Q1 \"total\""}))
    browser.perform(action({"name": "select_option", "element_id": "21", "option": "March"}))
    browser.perform(action({"name": "goto", "url": "https://crm.example.com/deals"}))
    browser.set_cookie("session_crm.example", "cookie_123", "crm.example")
    assert browser.busy() is True
    methods = [entry[1] for entry in devtools.log]
    assert methods[:2] == ["Page.addScriptToEvaluateOnNewDocument", "Runtime.evaluate"]
    fill_expression = devtools.log[3][2]["expression"]
    assert '[data-rlworld-id=\\"17\\"]' in fill_expression and '"Q1 \\"total\\""' in fill_expression
    sent = [(entry[1], entry[2]) for entry in devtools.log]
    assert ("Page.navigate", {"url": "https://crm.example.com/deals"}) in sent
    assert devtools.log[-2][2] == {"name": "session_crm.example", "value": "cookie_123", "domain": "crm.example",
                                   "path": "/", "secure": True, "httpOnly": True}


def test_new_tabs_attach_and_images_without_a_browser_stay_quiet():
    browser, devtools = devtools_browser([page("page_1"), page("page_2")])
    browser.perform(action({"name": "tab", "operation": "new", "url": "about:blank"}))
    assert browser.target_id == "page_2"
    assert ("ws://127.0.0.1:9222/devtools/page/page_1", "close", {}) in devtools.log
    offline = CdpBrowser("http://127.0.0.1:9", httpx.Client(transport=httpx.MockTransport(refuse_connection)))
    assert offline.dom() is None and offline.busy() is False
    with pytest.raises(GuestFailure, match="no browser"):
        offline.perform(action({"name": "back"}))


def refuse_connection(request):
    raise httpx.ConnectError("connection refused", request=request)


def test_entropy_payload_and_shell_commands():
    assert entropy_payload(b"abcd") == struct.pack("ii4s", 32, 4, b"abcd")
    assert user_command_argv("agent", "ls -la") == ["/usr/sbin/runuser", "-u", "agent", "--", "bash", "-lc", "ls -la"]


def test_blob_frames_travel_to_models_as_data_urls():
    object_store = InMemoryObjectStore()
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(output, format="WEBP")
    digest = put_blob(object_store, output.getvalue())
    resolver = BlobImageResolver(object_store)
    assert resolver.resolve(f"blob:{digest}").startswith("data:image/webp;base64,UklGR")
    assert resolver.resolve("https://frames.example/1.webp") == "https://frames.example/1.webp"


def test_file_snapshots_leave_out_app_profiles_and_app_stores(tmp_path):
    for relative_path in ("world/files/invoices.csv", ".thunderbird/rlworld/prefs.js",
                          "world/mail/current/inbox/new/m1", ".bashrc", "notes.txt"):
        (tmp_path / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative_path).write_text("x")
    snapshot = file_snapshot(tmp_path, frozenset({tmp_path / "world" / "mail"}))
    assert sorted(snapshot) == [".bashrc", "notes.txt", "world/files/invoices.csv"]
