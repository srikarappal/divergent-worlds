"""Serves accessibility trees as the desktop user (DESIGN.md section 6.3).

Applications accept AT-SPI peer connections only from their own user, so the root guest agent starts this
helper as the desktop user and asks it over a pipe: one JSON request per line in, one JSON reply per line out.
"""

import json
import sys
from typing import Any

import pyatspi

from rlworld.guest.accessibility_tree import accessibility_node


def tree_reply(max_depth: int) -> dict[str, Any]:
    desktop = pyatspi.Registry.getDesktop(0)
    if desktop is None:
        return {"tree": None}
    return {"tree": accessibility_node(desktop, "0", 0, max_depth).model_dump(mode="json")}


def main() -> None:
    for line in sys.stdin:
        request = json.loads(line)
        try:
            reply = tree_reply(int(request.get("max_depth", 40)))
        except Exception as error:  # a failing query answers with an error so the agent keeps its helper
            reply = {"error": f"{type(error).__name__}: {error}"}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
