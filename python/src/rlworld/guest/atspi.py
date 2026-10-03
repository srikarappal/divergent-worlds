"""AT-SPI accessibility on the guest desktop (DESIGN.md section 6.3); pyatspi comes from the image's packages."""

import pyatspi

from rlworld.actions.accessibility import AccessibilityNode
from rlworld.guest.accessibility_tree import accessibility_node


class AtspiAccessibility:
    def __init__(self, max_depth: int = 40) -> None:
        self.max_depth = max_depth

    def tree(self) -> AccessibilityNode | None:
        desktop = pyatspi.Registry.getDesktop(0)
        return accessibility_node(desktop, "0", 0, self.max_depth) if desktop is not None else None
