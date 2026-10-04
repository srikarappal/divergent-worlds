import base64
import hashlib
import os
import shlex
from pathlib import Path

import pytest
from support.guest_fakes import FakeClock, ScriptedShell, WindowsAppear, build_agent

from rlworld.actions.accessibility import AccessibilityNode
from rlworld.actions.models import TerminalOutput
from rlworld.guest import GuestFailure
from rlworld.guest import agent as agent_module
from rlworld.guest.agent import read_regular_file, window_titled
from rlworld.guest.protocol import READ_STEP_KINDS, TASK_READS_KEY
from rlworld.guest.system import UserShell, user_command_argv, user_probe_argv
from rlworld.worlds.generator import generate_mid_market_company


def encoded(data):
    return base64.b64encode(data).decode("ascii")


def test_setup_steps_run_in_order_after_the_windows(tmp_path):
    shell = ScriptedShell()
    clock = FakeClock()
    agent = build_agent(tmp_path, shell=shell, clock=clock)
    agent.handle("inject", {"open_windows": ["Calc"], "setup": [
        {"kind": "file", "path": "Desktop/data.bin", "base64": encoded(b"\x00\x01binary")},
        {"kind": "file", "path": "notes.txt", "text": "plain text"},
        {"kind": "launch", "command": "socat tcp-listen:9222,fork tcp:localhost:1337"},
        {"kind": "execute", "command": "python -c 'print(1)'"},
        {"kind": "execute", "command": "wmctrl -a Calc", "timeout_s": 5},
        {"kind": "sleep", "seconds": 2.5},
    ]})
    assert (agent.home / "Desktop" / "data.bin").read_bytes() == b"\x00\x01binary"
    assert (agent.home / "notes.txt").read_text() == "plain text"
    assert (agent.home / "notes.txt").stat().st_mode & 0o777 == 0o644
    assert shell.calls == [("launch", "libreoffice --calc"),
                           ("launch", "socat tcp-listen:9222,fork tcp:localhost:1337"),
                           ("execute", "python -c 'print(1)'", agent_module.EXECUTE_TIMEOUT_SECONDS),
                           ("execute", "wmctrl -a Calc", 5)]
    assert clock.sleeps == [2.5]
    assert agent.handle("observe", {"kinds": ["file_diff"]})["file_changes"] == []


def test_opening_a_file_waits_for_its_window(tmp_path):
    accessibility = WindowsAppear("report.xlsx - LibreOffice Calc", after_polls=3)
    shell = ScriptedShell()
    clock = FakeClock()
    agent = build_agent(tmp_path, shell=shell, accessibility=accessibility, clock=clock)
    agent.handle("inject", {"setup": [{"kind": "file", "path": "Desktop/report.xlsx", "text": "cells"},
                                      {"kind": "open", "path": "Desktop/report.xlsx"}]})
    assert shell.calls == [("open", (agent.home / "Desktop" / "report.xlsx").resolve())]
    assert accessibility.polls == 4
    assert clock.sleeps == [agent_module.WINDOW_POLL_SECONDS] * 3


def test_a_window_titled_without_the_extension_counts(tmp_path):
    agent = build_agent(tmp_path, accessibility=WindowsAppear("report - Document Viewer"))
    agent.handle("inject", {"setup": [{"kind": "file", "path": "report.pdf", "text": "%PDF"},
                                      {"kind": "open", "path": "report.pdf", "timeout_s": 5}]})


def test_setup_fails_when_the_opened_file_never_shows_a_window(tmp_path):
    clock = FakeClock()
    agent = build_agent(tmp_path, accessibility=WindowsAppear("Other window", after_polls=0), clock=clock)
    with pytest.raises(GuestFailure) as failure:
        agent.handle("inject", {"setup": [{"kind": "file", "path": "slides.pptx", "text": "x"},
                                          {"kind": "open", "path": "slides.pptx", "timeout_s": 4}]})
    assert failure.value.code == "setup_failed" and "slides.pptx" in str(failure.value)
    assert sum(clock.sleeps) == 4


class RecordingChown:
    def __init__(self):
        self.calls = []

    def __call__(self, path, uid, gid):
        self.calls.append(os.path.basename(path))


def test_folders_created_for_setup_files_belong_to_the_desktop_user(tmp_path, monkeypatch):
    chown = RecordingChown()
    monkeypatch.setattr(os, "chown", chown)
    agent = build_agent(tmp_path, owner=(1000, 1000))
    (agent.home / "Desktop").mkdir()
    agent.handle("inject", {"setup": [{"kind": "file", "path": "Desktop/project/data/a.csv", "text": "x"}],
                            "license_tokens": {"office": "token"}})
    assert chown.calls == ["office", "data", "project", "a.csv"]


@pytest.mark.parametrize("step, code", [
    ({"kind": "open", "path": "missing.docx"}, "setup_failed"),
    ({"kind": "open", "path": "../outside.txt"}, "bad_request"),
    ({"kind": "file", "path": "../../etc/profile.d/evil.sh", "text": "id"}, "bad_request"),
    ({"kind": "file", "path": "/etc/passwd", "text": "root::0:0"}, "bad_request"),
    ({"kind": "reboot"}, "bad_request"),
])
def test_setup_refuses_missing_files_escapes_and_unknown_steps(tmp_path, step, code):
    agent = build_agent(tmp_path)
    with pytest.raises(GuestFailure) as failure:
        agent.handle("inject", {"setup": [step]})
    assert failure.value.code == code


def read_plan():
    return {"before": [{"kind": "execute", "command": "python -c 'save()'", "timeout_s": 9},
                       {"kind": "sleep", "seconds": 0.5}],
            "files": ["Desktop/result.txt", "Desktop/never_written.txt"],
            "commands": ["which spotify"]}


def test_reads_run_their_steps_then_return_files_and_command_outputs(tmp_path):
    shell = ScriptedShell({"which spotify": TerminalOutput(stdout="/snap/bin/spotify\n", stderr="", exit_code=0)})
    clock = FakeClock()
    agent = build_agent(tmp_path, shell=shell, clock=clock)
    agent.handle("inject", {"setup": [{"kind": "file", "path": "Desktop/result.txt", "text": "draft"}],
                            "reads": read_plan()})
    (agent.home / "Desktop" / "result.txt").write_text("final answer")
    state = agent.handle("extract", {"apps": []})
    reads = state[TASK_READS_KEY]
    assert reads["files"]["Desktop/result.txt"] == {
        "base64": encoded(b"final answer"), "size": 12, "sha256": hashlib.sha256(b"final answer").hexdigest()}
    assert reads["files"]["Desktop/never_written.txt"]["missing"] is True
    assert reads["commands"] == {"which spotify": {"stdout": "/snap/bin/spotify\n", "stderr": "", "exit_code": 0}}
    assert shell.calls == [("probe", "python -c 'save()'", 9),
                           ("probe", "which spotify", agent_module.READ_TIMEOUT_SECONDS)]
    assert clock.sleeps == [0.5]


def test_the_read_plan_survives_later_injections_and_is_absent_without_one(tmp_path):
    agent = build_agent(tmp_path)
    assert TASK_READS_KEY not in agent.handle("extract", {"apps": []})
    agent.handle("inject", {"reads": {"files": ["a.txt"]}})
    agent.handle("inject", {"license_tokens": {"office": "token"}})
    assert agent.handle("extract", {"apps": []})[TASK_READS_KEY]["files"]["a.txt"]["missing"] is True


def test_reads_sit_beside_the_app_states(tmp_path):
    agent = build_agent(tmp_path)
    world = generate_mid_market_company(seed=3, size={"employees": 3, "customers": 2, "emails_per_person": 1})
    agent.handle("inject", {"world": world.model_dump(mode="json"), "apps": ["files"],
                            "reads": {"commands": ["date"]}})
    state = agent.handle("extract", {"apps": ["files"]})
    assert set(state) == {"files", TASK_READS_KEY}
    assert state[TASK_READS_KEY]["commands"]["date"]["exit_code"] == 127


@pytest.mark.parametrize("kind", ["launch", "open", "file"])
def test_read_plans_only_execute_and_sleep(tmp_path, kind):
    agent = build_agent(tmp_path)
    agent.handle("inject", {"reads": {"before": [{"kind": kind, "command": "x", "path": "a.txt", "text": "x"}]}})
    with pytest.raises(GuestFailure) as failure:
        agent.handle("extract", {"apps": []})
    assert failure.value.code == "bad_request" and kind in str(failure.value)
    assert READ_STEP_KINDS == frozenset({"execute", "sleep"})


def test_reads_never_follow_links_out_of_the_home_or_into_special_files(tmp_path):
    agent = build_agent(tmp_path)
    secret = tmp_path / "root_only_token"
    secret.write_text("dev container token")
    (agent.home / "inside.txt").write_text("inside")
    (agent.home / "to_secret.txt").symlink_to(secret)
    (agent.home / "to_inside.txt").symlink_to(agent.home / "inside.txt")
    os.mkfifo(agent.home / "pipe.txt")
    (agent.home / "folder.txt").mkdir()
    agent.handle("inject", {"reads": {"files": ["to_secret.txt", "to_inside.txt", "pipe.txt", "folder.txt",
                                                "../root_only_token"]}})
    files = agent.handle("extract", {"apps": []})[TASK_READS_KEY]["files"]
    assert files["to_secret.txt"]["missing"] and "escapes" in files["to_secret.txt"]["error"]
    assert files["../root_only_token"]["missing"]
    assert base64.b64decode(files["to_inside.txt"]["base64"]) == b"inside"
    assert files["pipe.txt"] == {"missing": True, "error": "not a regular file"}
    assert files["folder.txt"] == {"missing": True, "error": "not a regular file"}
    assert "dev container token" not in str(files)


def test_regular_file_reads_are_capped(tmp_path):
    path = tmp_path / "large.bin"
    path.write_bytes(b"x" * 11)
    assert read_regular_file(path, 11)["size"] == 11
    assert read_regular_file(path, 10) == {"missing": True, "error": "larger than 10 bytes"}
    assert read_regular_file(tmp_path / "absent.bin", 10)["missing"] is True


def test_command_outputs_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "MAX_READ_CHARACTERS", 5)
    shell = ScriptedShell({"cat log": TerminalOutput(stdout="0123456789", stderr="", exit_code=0)})
    agent = build_agent(tmp_path, shell=shell)
    agent.handle("inject", {"reads": {"commands": ["cat log"]}})
    output = agent.handle("extract", {"apps": []})[TASK_READS_KEY]["commands"]["cat log"]
    assert output["stdout"].startswith("01234\n[truncated 5")


def test_windows_are_found_by_role_and_title():
    dialog = AccessibilityNode(node_id="0.0.0", role="dialog", name="Save As")
    label = AccessibilityNode(node_id="0.0.1", role="label", name="Budget.xlsx")
    frame = AccessibilityNode(node_id="0.0", role="frame", name="Budget.xlsx - Calc", children=(dialog, label))
    root = AccessibilityNode(node_id="0", role="desktop frame", children=(frame,))
    assert window_titled(root, "Budget.xlsx") and window_titled(root, "Save")
    assert not window_titled(root, "Report") and not window_titled(None, "Budget")
    assert not window_titled(AccessibilityNode(node_id="0", role="desktop frame", children=(label,)), "Budget")


def test_probes_run_outside_the_login_shell():
    assert user_probe_argv("agent", "which spotify") == [
        "runuser", "-u", "agent", "--", "bash", "--noprofile", "--norc", "-c", "which spotify"]
    assert user_command_argv("agent", "ls")[-2:] == ["-lc", "ls"]


class LaunchRecordingShell(UserShell):
    """The desktop user's shell with its launches recorded instead of started."""

    def __init__(self):
        super().__init__("agent", "/home/agent")
        self.launched = []

    def launch(self, command):
        self.launched.append(command)


def test_the_linux_shell_opens_files_in_the_desktops_default_app():
    shell = LaunchRecordingShell()
    report = "/home/agent/Q3 report's.xlsx"
    shell.open_file(Path(report))
    assert shell.launched == [f"xdg-open {shlex.quote(report)}"]
