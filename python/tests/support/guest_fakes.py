"""Guest agent fakes for task setup and read plan tests: a shell that scripts probes, windows that appear late
and a clock that sleeps instantly."""

from PIL import Image

from rlworld.actions.accessibility import AccessibilityNode, Bounds
from rlworld.actions.models import TerminalOutput
from rlworld.guest import GuestAgent

VIEWPORT = Bounds(x=0, y=0, width=1280, height=800)


class BlankScreen:
    def capture(self):
        return Image.new("RGB", (1280, 800), "white")


class IgnoredRunner:
    def run(self, argv):
        pass


class NoBrowser:
    def dom(self):
        return None

    def busy(self):
        return False

    def perform(self, action):
        pass

    def set_cookie(self, name, value, domain):
        pass


class QuietHygiene:
    def set_clock(self, when):
        pass

    def add_entropy(self, entropy):
        pass


class ScriptedShell:
    """Records every call in order; probes answer from a script of outputs keyed by command."""

    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = outputs or {}

    def execute(self, command, timeout_s):
        self.calls.append(("execute", command, timeout_s))
        return TerminalOutput(stdout="", stderr="", exit_code=1)

    def probe(self, command, timeout_s):
        self.calls.append(("probe", command, timeout_s))
        return self.outputs.get(command, TerminalOutput(stdout="", stderr="not found", exit_code=127))

    def launch(self, command):
        self.calls.append(("launch", command))

    def open_file(self, path):
        self.calls.append(("open", path))


class WindowsAppear:
    """No windows for the first polls, then a frame with the given title."""

    def __init__(self, title, after_polls=0):
        self.title = title
        self.after_polls = after_polls
        self.polls = 0

    def tree(self):
        self.polls += 1
        windows = ()
        if self.polls > self.after_polls:
            windows = (AccessibilityNode(node_id="0.0", role="frame", name=self.title,
                                         bounds=Bounds(x=0, y=0, width=1280, height=800)),)
        return AccessibilityNode(node_id="0", role="desktop frame", children=windows)


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def build_agent(tmp_path, shell=None, accessibility=None, clock=None, owner=None, world_inside_home=False,
                native=None, integrations=None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    clock = clock or FakeClock()
    return GuestAgent(
        screen=BlankScreen(), input_runner=IgnoredRunner(), accessibility=accessibility or WindowsAppear("Files"),
        browser=NoBrowser(), shell=shell or ScriptedShell(), hygiene=QuietHygiene(), home=home,
        world_root=home / "world" if world_inside_home else tmp_path / "world", license_root=tmp_path / "licenses",
        secrets_path=tmp_path / "run" / "episode.env", launchers={"Calc": "libreoffice --calc"}, viewport=VIEWPORT,
        owner=owner, sleep=clock.sleep, monotonic=clock.monotonic, native=native, integrations=integrations)
