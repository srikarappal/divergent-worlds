"""System backends of the Windows in session helper (DESIGN.md sections 6.1 and 6.3), apart from COM so they load
anywhere.

The helper runs as the autologon user in the interactive session, so the
policy's commands run as that user without switching accounts: PowerShell with
the user's profile for terminal actions and without it for task read probes,
since the policy may have edited the profile. Screens, input, the clock and RNG
reseeding belong to the hypervisor on Windows, so those backends refuse. App
launchers come from the image's launchers file.
"""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image

from rlworld.actions.models import TerminalOutput
from rlworld.guest.commands import run_command
from rlworld.guest.protocol import GuestFailure

POWERSHELL = "powershell.exe"
# Launched apps get their own process group and no console, so they outlive the helper's.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def powershell_argv(command: str, profile: bool) -> list[str]:
    argv = [POWERSHELL, "-NoLogo", "-NonInteractive"]
    if not profile:
        argv.append("-NoProfile")
    return [*argv, "-Command", command]


def launcher_commands(path: Path) -> dict[str, str]:
    """The image's launchers file, {window: {executable, arguments}}, as command lines by window."""
    launchers = json.loads(path.read_text(encoding="utf-8"))
    return {window: subprocess.list2cmdline([launcher["executable"], *launcher.get("arguments", [])])
            for window, launcher in launchers.items()}


class WindowsShell:
    """The policy's shell: PowerShell as the session's user."""

    def execute(self, command: str, timeout_s: int) -> TerminalOutput:
        return run_command(powershell_argv(command, profile=True), timeout_s)

    def probe(self, command: str, timeout_s: int) -> TerminalOutput:
        return run_command(powershell_argv(command, profile=False), timeout_s)

    def launch(self, command: str) -> None:
        subprocess.Popen(command, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_file(self, path: Path) -> None:
        """Opens a file in the app Windows associates with its type."""
        os.startfile(path)


class HypervisorIO:
    """Screens and input belong to the hypervisor; the host never asks the helper for them."""

    def capture(self) -> Image.Image:
        raise GuestFailure("unsupported_action", "the hypervisor captures Windows screens")

    def run(self, argv: list[str]) -> None:
        raise GuestFailure("unsupported_action", "the hypervisor plays computer actions on Windows")


class HypervisorHygiene:
    """The QEMU guest agent sets the clock and every clone's VM generation id reseeds Windows' RNG."""

    def set_clock(self, when: datetime) -> None:
        raise GuestFailure("unsupported_action", "the QEMU guest agent sets the Windows clock")

    def add_entropy(self, entropy: bytes) -> None:
        raise GuestFailure("unsupported_action", "the VM generation id reseeds the Windows RNG")
