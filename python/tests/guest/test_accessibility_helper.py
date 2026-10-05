import sys

import pytest

from rlworld.actions.accessibility import AccessibilityNode
from rlworld.guest.atspi import AtspiAccessibility
from rlworld.guest.protocol import GuestFailure

TREE = {"node_id": "0", "role": "desktop frame", "children": [{"node_id": "0.0", "role": "application",
                                                               "name": "soffice"}]}
ANSWERING = f"import json, sys\nfor line in sys.stdin:\n    print(json.dumps({{'tree': {TREE!r}}}), flush=True)\n"
FAILING = "import json, sys\nfor line in sys.stdin:\n    print(json.dumps({'error': 'no desktop'}), flush=True)\n"
EXITING = "import sys\nsys.stdin.readline()\n"


def scripted(script):
    accessibility = AtspiAccessibility("agent", "unix:path=/run/rlworld-desktop/bus", ":0", timeout_s=10.0)
    accessibility.argv = [sys.executable, "-c", script]
    return accessibility


def test_the_helper_runs_as_the_desktop_user_on_the_desktop_session_bus():
    accessibility = AtspiAccessibility("agent", "unix:path=/run/rlworld-desktop/bus", ":0")
    assert accessibility.argv[:4] == ["/usr/sbin/runuser", "-u", "agent", "--"]
    assert accessibility.argv[-2:] == ["-m", "rlworld.guest.accessibility_helper"]
    assert accessibility.environment["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/rlworld-desktop/bus"


def test_trees_come_back_from_one_helper_kept_across_requests():
    accessibility = scripted(ANSWERING)
    assert accessibility.tree() == AccessibilityNode.model_validate(TREE)
    first_helper = accessibility.process
    assert accessibility.tree().children[0].name == "soffice" and accessibility.process is first_helper
    accessibility.stop_helper(first_helper)


def test_helper_errors_and_exits_are_guest_failures_and_an_exited_helper_starts_again():
    failing = scripted(FAILING)
    with pytest.raises(GuestFailure) as failure:
        failing.tree()
    assert failure.value.code == "accessibility_unavailable" and "no desktop" in str(failure.value)
    failing.stop_helper(failing.process)
    exiting = scripted(EXITING)
    with pytest.raises(GuestFailure):
        exiting.tree()
    exited_helper = exiting.process
    exiting.argv = [sys.executable, "-c", ANSWERING]
    assert exiting.tree() is not None and exiting.process is not exited_helper
    exiting.stop_helper(exiting.process)
