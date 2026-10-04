import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from support.guest_fakes import NoBrowser, ScriptedShell, WindowsAppear

from rlworld.actions.accessibility import Bounds
from rlworld.actions.models import TerminalOutput
from rlworld.guest import GuestAgent, GuestFailure
from rlworld.guest import agent as agent_module
from rlworld.guest import server as server_module
from rlworld.guest import windows_system
from rlworld.guest.server import SerialPortServer
from rlworld.guest.windows_options import HELPER_PORT, parse_arguments
from rlworld.guest.windows_system import (
    HypervisorHygiene,
    HypervisorIO,
    WindowsShell,
    launcher_commands,
    powershell_argv,
)

EXCEL = "C:\\Program Files\\Microsoft Office\\root\\Office16\\EXCEL.EXE"


def test_terminal_commands_run_in_powershell_and_probes_skip_the_profile():
    assert powershell_argv("Get-ChildItem", profile=True) == [
        "powershell.exe", "-NoLogo", "-NonInteractive", "-Command", "Get-ChildItem"]
    assert powershell_argv("Test-Path $env:TEMP", profile=False) == [
        "powershell.exe", "-NoLogo", "-NonInteractive", "-NoProfile", "-Command", "Test-Path $env:TEMP"]


class RecordedCommands:
    def __init__(self):
        self.runs = []
        self.started = []
        self.opened = []

    def run_command(self, argv, timeout_s, environment=None):
        self.runs.append((argv, timeout_s, environment))
        return TerminalOutput(stdout="ok", stderr="", exit_code=0)

    def popen(self, command, **options):
        self.started.append((command, options))

    def startfile(self, path):
        self.opened.append(path)


def test_the_windows_shell_runs_as_the_session_user(monkeypatch):
    recorded = RecordedCommands()
    monkeypatch.setattr(windows_system, "run_command", recorded.run_command)
    monkeypatch.setattr(windows_system.subprocess, "Popen", recorded.popen)
    monkeypatch.setattr(windows_system.os, "startfile", recorded.startfile, raising=False)
    shell = WindowsShell()
    assert shell.execute("dir", 30).stdout == "ok"
    shell.probe("Get-Content result.txt", 60)
    assert recorded.runs == [(powershell_argv("dir", profile=True), 30, None),
                             (powershell_argv("Get-Content result.txt", profile=False), 60, None)]
    shell.launch(f'"{EXCEL}" /x')
    command, options = recorded.started[0]
    assert command == f'"{EXCEL}" /x'
    assert options["creationflags"] == windows_system.DETACHED_PROCESS | windows_system.CREATE_NEW_PROCESS_GROUP
    assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    shell.open_file(Path("C:/Users/agent/Documents/budget.xlsx"))
    assert recorded.opened == [Path("C:/Users/agent/Documents/budget.xlsx")]


def test_the_images_launchers_file_becomes_command_lines(tmp_path):
    launchers = tmp_path / "launchers.json"
    launchers.write_text(json.dumps({"Excel": {"executable": EXCEL, "arguments": ["/x"]},
                                     "Edge": {"executable": "msedge.exe", "arguments": []},
                                     "Notepad": {"executable": "notepad.exe"}}))
    assert launcher_commands(launchers) == {"Excel": f'"{EXCEL}" /x', "Edge": "msedge.exe",
                                            "Notepad": "notepad.exe"}


def test_screens_input_and_clone_hygiene_belong_to_the_hypervisor():
    hypervisor = HypervisorIO()
    hygiene = HypervisorHygiene()
    refusals = [hypervisor.capture, hygiene.set_clock, hygiene.add_entropy]
    arguments = [(), (None,), (b"seed",)]
    for refusal, argument in zip(refusals, arguments, strict=True):
        with pytest.raises(GuestFailure) as failure:
            refusal(*argument)
        assert failure.value.code == "unsupported_action"
    with pytest.raises(GuestFailure, match="hypervisor plays computer actions"):
        hypervisor.run(["xdotool", "click", "1"])


def test_helper_options_default_to_the_drivers_port_and_the_users_local_state(tmp_path):
    arguments = parse_arguments(["--launchers", "C:/rlworld/launchers.json"])
    local_state = Path.home() / "AppData" / "Local" / "rlworld"
    assert arguments.port == HELPER_PORT == r"\\.\Global\org.rlworld.helper.0"
    assert (arguments.launchers, arguments.world_root) == (Path("C:/rlworld/launchers.json"), None)
    assert (arguments.license_root, arguments.secrets_path) == (local_state / "licenses", local_state / "episode.env")
    assert arguments.cdp_endpoint == "http://127.0.0.1:9222"
    with pytest.raises(SystemExit):
        parse_arguments([])


def test_file_reads_never_follow_links_or_block():
    assert agent_module.READ_FLAGS & os.O_NOFOLLOW and agent_module.READ_FLAGS & os.O_NONBLOCK
    assert agent_module.READ_FLAGS & os.O_ACCMODE == os.O_RDONLY


class FakePort(io.RawIOBase):
    """The guest end of a virtio serial port: what the host sent, then an empty read once it hung up."""

    def __init__(self, incoming):
        self.incoming = io.BytesIO(incoming)
        self.outgoing = bytearray()

    def readable(self):
        return True

    def writable(self):
        return True

    def readinto(self, buffer):
        chunk = self.incoming.read(len(buffer))
        buffer[:len(chunk)] = chunk
        return len(chunk)

    def write(self, data):
        self.outgoing += data
        return len(data)


class PortOpener:
    """Hands out ports in order; an exception in the list is a port that failed to open."""

    def __init__(self, ports):
        self.ports = list(ports)
        self.opened = []

    def open(self, path, mode, buffering):
        self.opened.append((path, mode, buffering))
        port = self.ports.pop(0)
        if isinstance(port, Exception):
            raise port
        return port


class EchoHandler:
    def handle(self, method, params):
        if method == "explode":
            raise GuestFailure("internal", "the backend failed")
        return {"method": method, "params": params}


class StopAfterIdle:
    """Records sleeps and stops the server at its first idle read, once the host has hung up."""

    def __init__(self):
        self.calls = []
        self.server = None

    def sleep(self, seconds):
        self.calls.append(seconds)
        if seconds == server_module.SERIAL_IDLE_SECONDS:
            self.server.stop()


def request(request_id, method, params=None):
    return json.dumps({"id": request_id, "method": method, "params": params or {}}).encode() + b"\n"


def test_the_serial_port_server_answers_the_host_and_reopens_a_failed_port():
    port = FakePort(request(7, "observe", {"kinds": []}) + request(8, "explode") + b"not json\n")
    opener = PortOpener([OSError("the port is not there yet"), port])
    pacing = StopAfterIdle()
    server = SerialPortServer(EchoHandler(), HELPER_PORT, opener=opener.open, sleep=pacing.sleep)
    pacing.server = server
    server.serve_forever()
    assert opener.opened == [(HELPER_PORT, "r+b", 0)] * 2
    assert pacing.calls == [server_module.SERIAL_RETRY_SECONDS, server_module.SERIAL_IDLE_SECONDS]
    replies = [json.loads(line) for line in port.outgoing.decode().splitlines()]
    assert replies[0] == {"id": 7, "result": {"method": "observe", "params": {"kinds": []}}}
    assert replies[1] == {"id": 8, "error": {"code": "internal", "message": "the backend failed"}}
    assert replies[2]["id"] is None and replies[2]["error"]["code"] == "bad_request"
    assert port.closed


def test_an_idle_port_is_read_again_until_the_host_writes():
    port = FakePort(b"")
    pacing = StopAfterIdle()
    server = SerialPortServer(EchoHandler(), HELPER_PORT, opener=PortOpener([port]).open, sleep=pacing.sleep)
    pacing.server = server
    server.serve_forever()
    assert pacing.calls == [server_module.SERIAL_IDLE_SECONDS] and port.outgoing == b""


def helper_agent(tmp_path, shell):
    """The guest agent wired the way the Windows helper's entry point wires it, with fakes for COM."""
    hypervisor = HypervisorIO()
    home = tmp_path / "home"
    home.mkdir()
    return GuestAgent(screen=hypervisor, input_runner=hypervisor, accessibility=WindowsAppear("budget.xlsx - Excel"),
                      browser=NoBrowser(), shell=shell, hygiene=HypervisorHygiene(), home=home,
                      world_root=home / "world", license_root=tmp_path / "licenses",
                      secrets_path=tmp_path / "episode.env", launchers={"Excel": f'"{EXCEL}"'},
                      viewport=Bounds(x=0, y=0, width=1280, height=800))


def test_the_helper_serves_setup_and_terminal_work_and_refuses_the_hypervisors(tmp_path):
    shell = ScriptedShell()
    agent = helper_agent(tmp_path, shell)
    agent.handle("inject", {"open_windows": ["Excel"], "setup": [
        {"kind": "file", "path": "Documents/budget.xlsx", "text": "cells"},
        {"kind": "open", "path": "Documents/budget.xlsx"}]})
    assert shell.calls == [("launch", f'"{EXCEL}"'), ("open", (agent.home / "Documents" / "budget.xlsx").resolve())]
    agent.handle("act", {"action": {"name": "exec", "command": "Get-Date"}})
    assert agent.handle("observe", {"kinds": []})["terminal"]["exit_code"] == 1
    for refused in (("act", {"action": {"name": "click", "x": 1, "y": 1}}), ("prepare", {"now": "2026-03-14T09:00"}),
                    ("screen_hash", {}), ("observe", {"kinds": ["screenshot"]})):
        with pytest.raises(GuestFailure) as failure:
            agent.handle(*refused)
        assert failure.value.code == "unsupported_action"
