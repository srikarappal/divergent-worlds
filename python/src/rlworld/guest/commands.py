"""Running one command for the policy or a task read (DESIGN.md section 6.3), on Linux and Windows guests alike.

Output is captured as text; a command past its time limit is stopped and
reports the timeout's conventional exit code.
"""

import subprocess
from collections.abc import Mapping, Sequence

from rlworld.actions.models import TerminalOutput

TIMED_OUT_EXIT_CODE = 124


def run_command(argv: Sequence[str], timeout_s: int, environment: Mapping[str, str] | None = None) -> TerminalOutput:
    try:
        completed = subprocess.run(list(argv), env=environment, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode() if isinstance(error.stdout, bytes) else error.stdout or ""
        return TerminalOutput(stdout=stdout, stderr=f"timed out after {timeout_s} s", exit_code=TIMED_OUT_EXIT_CODE)
    return TerminalOutput(stdout=completed.stdout, stderr=completed.stderr, exit_code=completed.returncode)
