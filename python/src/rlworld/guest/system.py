"""Guest system backends (DESIGN.md sections 6.1 and 6.3): the X display, clone hygiene and the policy's shell.

Input and screenshots go through the X display the apps draw on. The shell
runs commands as the unprivileged desktop user, never as root. Clone hygiene
sets the clock to the world's time and credits fresh entropy to the kernel RNG,
since every clone restores the same RNG state.
"""

import fcntl
import os
import struct
import subprocess
import time
from datetime import datetime

from PIL import Image, ImageGrab

from rlworld.actions.models import TerminalOutput

# _IOW('R', 0x03, int[2]) on Linux: add entropy and credit it.
RNDADDENTROPY = 0x40085203
TIMED_OUT_EXIT_CODE = 124


class X11Screen:
    def __init__(self, display: str = ":0") -> None:
        self.display = display

    def capture(self) -> Image.Image:
        return ImageGrab.grab(xdisplay=self.display)


class DisplayCommandRunner:
    """Runs xdotool against the desktop's display."""

    def __init__(self, display: str = ":0", timeout_seconds: float = 30.0) -> None:
        self.environment = {**os.environ, "DISPLAY": display}
        self.timeout_seconds = timeout_seconds

    def run(self, argv: list[str]) -> None:
        subprocess.run(argv, env=self.environment, check=True, capture_output=True, timeout=self.timeout_seconds)


def entropy_payload(entropy: bytes) -> bytes:
    """struct rand_pool_info: entropy bits credited, byte count, then the bytes."""
    return struct.pack(f"ii{len(entropy)}s", len(entropy) * 8, len(entropy), entropy)


class SystemHygiene:
    def set_clock(self, when: datetime) -> None:
        time.clock_settime(time.CLOCK_REALTIME, when.timestamp())

    def add_entropy(self, entropy: bytes) -> None:
        with open("/dev/urandom", "wb") as random_device:
            fcntl.ioctl(random_device, RNDADDENTROPY, entropy_payload(entropy))


def user_command_argv(user: str, command: str) -> list[str]:
    return ["runuser", "-u", user, "--", "bash", "-lc", command]


class UserShell:
    """The policy's shell: commands and app launches run as the desktop user."""

    def __init__(self, user: str, home: str, display: str = ":0") -> None:
        self.user = user
        self.environment = {"HOME": home, "USER": user, "DISPLAY": display,
                            "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "en_US.UTF-8"}

    def execute(self, command: str, timeout_s: int) -> TerminalOutput:
        try:
            completed = subprocess.run(user_command_argv(self.user, command), env=self.environment,
                                       capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout or ""
            return TerminalOutput(stdout=stdout, stderr=f"timed out after {timeout_s} s",
                                  exit_code=TIMED_OUT_EXIT_CODE)
        return TerminalOutput(stdout=completed.stdout, stderr=completed.stderr, exit_code=completed.returncode)

    def launch(self, command: str) -> None:
        subprocess.Popen(user_command_argv(self.user, command), env=self.environment, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
