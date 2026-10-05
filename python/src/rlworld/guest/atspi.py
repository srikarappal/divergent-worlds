"""AT-SPI accessibility on the guest desktop (DESIGN.md section 6.3).

The agent runs as root and applications answer AT-SPI only to their own user, so the trees come from the
accessibility helper, started as the desktop user on first use and again whenever it has exited.
"""

import json
import select
import subprocess
import sys

from rlworld.actions.accessibility import AccessibilityNode
from rlworld.guest.protocol import GuestFailure
from rlworld.guest.system import RUNUSER


class AtspiAccessibility:
    def __init__(self, user: str, session_bus: str, display: str, max_depth: int = 40,
                 timeout_s: float = 30.0) -> None:
        self.argv = [RUNUSER, "-u", user, "--", sys.executable, "-m", "rlworld.guest.accessibility_helper"]
        self.environment = {"DBUS_SESSION_BUS_ADDRESS": session_bus, "DISPLAY": display, "PATH": "/usr/bin:/bin"}
        self.max_depth = max_depth
        self.timeout_s = timeout_s
        self.process: subprocess.Popen | None = None

    def helper(self) -> subprocess.Popen:
        if self.process is None or self.process.poll() is not None:
            self.process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=self.environment, text=True)
        return self.process

    def stop_helper(self, process: subprocess.Popen) -> None:
        process.kill()
        process.wait()

    def tree(self) -> AccessibilityNode | None:
        process = self.helper()
        try:
            process.stdin.write(json.dumps({"max_depth": self.max_depth}) + "\n")
            process.stdin.flush()
        except OSError as error:
            raise GuestFailure("accessibility_unavailable", f"the accessibility helper exited: {error}") from error
        readable, _, _ = select.select([process.stdout], [], [], self.timeout_s)
        line = process.stdout.readline() if readable else ""
        if not line:
            self.stop_helper(process)
            raise GuestFailure("accessibility_unavailable", "the accessibility helper did not answer")
        reply = json.loads(line)
        if "error" in reply:
            raise GuestFailure("accessibility_unavailable", reply["error"])
        return AccessibilityNode.model_validate(reply["tree"]) if reply["tree"] is not None else None
