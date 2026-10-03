"""The Linux guest agent (DESIGN.md sections 6.1, 6.3, 6.4 and 6.5).

It runs as root inside the guest and answers only the host over vsock, so the
policy's unprivileged shell never reaches it. It plays canonical actions,
observes the screen, the pruned accessibility tree, the DOM, terminal output
and file changes, hashes the screen for settle detection, applies clone
hygiene after a restore (world clock and fresh RNG entropy), writes world
state, task setup and entitlements, then extracts app state for the verifiers.
Backends hide X11, AT-SPI, Chrome DevTools and the shell.
"""

import base64
import hashlib
import io
import itertools
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from rlworld.actions.accessibility import AccessibilityNode, Bounds, prune_accessibility_tree
from rlworld.actions.keys import XDOTOOL_NAMES, render_xdotool_combo
from rlworld.actions.models import (
    ActionModel,
    ClickAction,
    DragAction,
    KeyAction,
    MoveAction,
    ScrollAction,
    TerminalOutput,
    TypeAction,
    WaitAction,
    parse_action,
)
from rlworld.guest.protocol import GuestFailure
from rlworld.worlds.integrations import extract_world_state, materialize_world
from rlworld.worlds.model import World

BUTTON_NUMBERS = {"left": "1", "middle": "2", "right": "3"}
HASH_SIZE = (160, 100)
SESSION_COOKIE_PREFIX = "session_"


class Screen(Protocol):
    def capture(self) -> Image.Image: ...


class CommandRunner(Protocol):
    def run(self, argv: list[str]) -> None: ...


class Accessibility(Protocol):
    def tree(self) -> AccessibilityNode | None: ...


class Browser(Protocol):
    def dom(self) -> str | None: ...

    def busy(self) -> bool: ...

    def perform(self, action: ActionModel) -> None: ...

    def set_cookie(self, name: str, value: str, domain: str) -> None: ...


class Shell(Protocol):
    def execute(self, command: str, timeout_s: int) -> TerminalOutput: ...

    def launch(self, command: str) -> None: ...


class Hygiene(Protocol):
    def set_clock(self, when: datetime) -> None: ...

    def add_entropy(self, entropy: bytes) -> None: ...


def xdotool_argv(action: ActionModel, typing_delay_ms: int) -> list[str]:
    """One xdotool invocation per computer action; --sync waits until the pointer arrives."""
    if isinstance(action, MoveAction):
        return ["xdotool", "mousemove", "--sync", str(action.x), str(action.y)]
    if isinstance(action, ClickAction):
        modifiers = [XDOTOOL_NAMES.get(modifier, modifier) for modifier in action.modifiers]
        argv = ["xdotool"]
        for modifier in modifiers:
            argv += ["keydown", modifier]
        argv += ["mousemove", "--sync", str(action.x), str(action.y), "click", "--repeat", str(action.count),
                 BUTTON_NUMBERS[action.button]]
        for modifier in reversed(modifiers):
            argv += ["keyup", modifier]
        return argv
    if isinstance(action, DragAction):
        start, *rest = action.path
        argv = ["xdotool", "mousemove", "--sync", str(start.x), str(start.y), "mousedown",
                BUTTON_NUMBERS[action.button]]
        for point in rest:
            argv += ["mousemove", "--sync", str(point.x), str(point.y)]
        return argv + ["mouseup", BUTTON_NUMBERS[action.button]]
    if isinstance(action, ScrollAction):
        argv = ["xdotool", "mousemove", "--sync", str(action.x), str(action.y)]
        if action.delta_y:
            argv += ["click", "--repeat", str(abs(action.delta_y)), "5" if action.delta_y > 0 else "4"]
        if action.delta_x:
            argv += ["click", "--repeat", str(abs(action.delta_x)), "7" if action.delta_x > 0 else "6"]
        return argv
    if isinstance(action, TypeAction):
        return ["xdotool", "type", "--delay", str(typing_delay_ms), "--", action.text]
    if isinstance(action, KeyAction):
        return ["xdotool", "key", "--", render_xdotool_combo(action.keys)]
    raise GuestFailure("unsupported_action", f"{action.name} is not a computer action")


def cookie_domain(cookie_name: str) -> str:
    """Session cookies minted per episode are named session_<domain>."""
    if not cookie_name.startswith(SESSION_COOKIE_PREFIX):
        raise GuestFailure("bad_request", f"cookie {cookie_name} does not name its domain")
    return cookie_name.removeprefix(SESSION_COOKIE_PREFIX)


def file_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    snapshot = {}
    for directory, _, file_names in os.walk(root):
        for file_name in file_names:
            file_path = Path(directory) / file_name
            try:
                stat = file_path.stat()
            except FileNotFoundError:
                continue
            snapshot[file_path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def file_changes(before: Mapping[str, tuple[int, int]], after: Mapping[str, tuple[int, int]]) -> list[dict[str, str]]:
    changes = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            changes.append({"path": path, "change": "created"})
        elif path not in after:
            changes.append({"path": path, "change": "deleted"})
        elif before[path] != after[path]:
            changes.append({"path": path, "change": "modified"})
    return changes


def contained_path(root: Path, relative_path: str) -> Path:
    """Injected files land inside the user's home, never anywhere else."""
    target = (root / relative_path).resolve()
    if os.path.isabs(relative_path) or not target.is_relative_to(root.resolve()):
        raise GuestFailure("bad_request", f"{relative_path} escapes the home directory")
    return target


def truncated(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n[truncated {len(text) - limit} characters]"


class GuestAgent:
    def __init__(
        self,
        screen: Screen,
        input_runner: CommandRunner,
        accessibility: Accessibility,
        browser: Browser,
        shell: Shell,
        hygiene: Hygiene,
        home: Path,
        world_root: Path,
        license_root: Path,
        secrets_path: Path,
        launchers: Mapping[str, str],
        viewport: Bounds,
        owner: tuple[int, int] | None = None,
        typing_delay_ms: int = 12,
        max_output_characters: int = 16_384,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.screen = screen
        self.input_runner = input_runner
        self.accessibility = accessibility
        self.browser = browser
        self.shell = shell
        self.hygiene = hygiene
        self.home = home
        self.world_root = world_root
        self.license_root = license_root
        self.secrets_path = secrets_path
        self.launchers = dict(launchers)
        self.viewport = viewport
        self.owner = owner
        self.typing_delay_ms = typing_delay_ms
        self.max_output_characters = max_output_characters
        self.sleep = sleep
        self.pending_terminal: TerminalOutput | None = None
        self.baseline = file_snapshot(home)
        self.busy_marks = itertools.count()
        self.methods: dict[str, Callable[[dict[str, Any]], Any]] = {
            "health": self.health,
            "prepare": self.prepare,
            "act": self.act,
            "screen_hash": self.screen_hash,
            "observe": self.observe,
            "inject": self.inject,
            "extract": self.extract,
        }

    def handle(self, method: str, params: dict[str, Any]) -> Any:
        if method not in self.methods:
            raise GuestFailure("unknown_method", f"the guest agent has no method {method}")
        return self.methods[method](params)

    def health(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    def prepare(self, params: dict[str, Any]) -> dict[str, Any]:
        """Clone hygiene before each turn: the world clock always, fresh entropy once after a restore."""
        self.hygiene.set_clock(datetime.fromisoformat(params["now"]))
        if params.get("entropy"):
            self.hygiene.add_entropy(base64.b64decode(params["entropy"]))
        return {}

    def act(self, params: dict[str, Any]) -> dict[str, Any]:
        action = parse_action(params["action"])
        if action.modality == "computer":
            if isinstance(action, WaitAction):
                self.sleep(action.duration_ms / 1000)
            else:
                self.input_runner.run(xdotool_argv(action, self.typing_delay_ms))
        elif action.modality == "browser":
            self.browser.perform(action)
        elif action.modality == "terminal":
            output = self.shell.execute(action.command, action.timeout_s)
            self.pending_terminal = TerminalOutput(stdout=truncated(output.stdout, self.max_output_characters),
                                                   stderr=truncated(output.stderr, self.max_output_characters),
                                                   exit_code=output.exit_code)
        elif action.modality == "native":
            raise GuestFailure("unsupported_action", "this Linux image exposes no native app API")
        else:
            raise GuestFailure("unsupported_action", "control actions are handled by the rollout worker")
        return {}

    def screen_hash(self, params: dict[str, Any]) -> str:
        """A coarse hash for settle detection; while pages still load it never repeats, so nothing settles early."""
        thumbnail = self.screen.capture().convert("L").resize(HASH_SIZE)
        digest = hashlib.sha256(thumbnail.tobytes()).hexdigest()
        return f"{digest}:busy:{next(self.busy_marks)}" if self.browser.busy() else digest

    def observe(self, params: dict[str, Any]) -> dict[str, Any]:
        kinds = set(params.get("kinds", ["screenshot", "accessibility_tree"]))
        observation: dict[str, Any] = {"step_index": 0}
        if "screenshot" in kinds:
            image = self.screen.capture()
            output = io.BytesIO()
            image.convert("RGB").save(output, format="WEBP", quality=80)
            observation["screenshot_data"] = base64.b64encode(output.getvalue()).decode("ascii")
            observation["screenshot_size"] = list(image.size)
        if "accessibility_tree" in kinds:
            root = self.accessibility.tree()
            nodes = prune_accessibility_tree(root, self.viewport) if root is not None else []
            observation["accessibility"] = [node.model_dump(mode="json") for node in nodes]
        if "dom" in kinds:
            observation["dom"] = self.browser.dom()
        if self.pending_terminal is not None:
            observation["terminal"] = self.pending_terminal.model_dump()
            self.pending_terminal = None
        if "file_diff" in kinds:
            observation["file_changes"] = file_changes(self.baseline, file_snapshot(self.home))
        return observation

    def write_owned(self, target: Path, data: bytes, mode: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        if self.owner is not None:
            os.chown(target, *self.owner)

    def inject(self, params: dict[str, Any]) -> dict[str, Any]:
        """World state, task setup and entitlements; file changes are counted from here on."""
        if "world" in params:
            materialize_world(World.model_validate(params["world"]), params.get("apps", []), self.world_root)
        for relative_path, content in params.get("files", {}).items():
            data = base64.b64decode(content["base64"]) if "base64" in content else content["text"].encode("utf-8")
            self.write_owned(contained_path(self.home, relative_path), data, 0o644)
        for product, token in params.get("license_tokens", {}).items():
            self.write_owned(self.license_root / product, token.encode("utf-8"), 0o644)
        if params.get("environment"):
            lines = "".join(f"{name}={value}\n" for name, value in sorted(params["environment"].items()))
            self.write_owned(self.secrets_path, lines.encode("utf-8"), 0o600)
        for name, value in params.get("browser_cookies", {}).items():
            self.browser.set_cookie(name, value, cookie_domain(name))
        for window in params.get("open_windows", []):
            if window not in self.launchers:
                raise GuestFailure("bad_request", f"no launcher for window {window}")
            self.shell.launch(self.launchers[window])
        self.baseline = file_snapshot(self.home)
        return {}

    def extract(self, params: dict[str, Any]) -> dict[str, Any]:
        return extract_world_state(params.get("apps", []), self.world_root)
