"""COM and Win32 backends of the Windows in session helper (DESIGN.md section 6.3); they load only on Windows.

UI Automation reads the session's tree with one cache request per
observation over the control view, rather than a cross process call per
property. Excel is the instance the session's user runs, since Microsoft does
not support Office automation from services. The helper is per monitor DPI
aware, so UIA rectangles are in the pixels of the hypervisor's screendumps.
"""

import ctypes
from collections.abc import Sequence
from typing import Any

import comtypes.client
import pywintypes
import win32com.client

from rlworld.actions.accessibility import AccessibilityNode, Bounds
from rlworld.guest.uia import CACHED_PROPERTIES, uia_node

uia_client = comtypes.client.GetModule("UIAutomationCore.dll")
TREE_SCOPE_SUBTREE = 7
PER_MONITOR_DPI_AWARE = 2
SCREEN_WIDTH_METRIC = 0
SCREEN_HEIGHT_METRIC = 1
# Errors Excel's COM objects raise, which native calls turn into refusals the agent reads.
COM_ERRORS = (pywintypes.com_error,)


class CachedElement:
    """An element of the cached tree; reading it makes no cross process call."""

    def __init__(self, element: Any) -> None:
        self.element = element

    def property_value(self, property_id: int) -> Any:
        return self.element.GetCachedPropertyValue(property_id)

    def children(self) -> Sequence["CachedElement"]:
        cached = self.element.GetCachedChildren()
        if not cached:
            return []
        return [CachedElement(cached.GetElement(index)) for index in range(cached.Length)]


class UiaAccessibility:
    def __init__(self, max_depth: int = 40) -> None:
        self.automation = comtypes.client.CreateObject(uia_client.CUIAutomation, interface=uia_client.IUIAutomation)
        self.cache = self.automation.CreateCacheRequest()
        for property_id in CACHED_PROPERTIES:
            self.cache.AddProperty(property_id)
        self.cache.TreeScope = TREE_SCOPE_SUBTREE
        self.cache.TreeFilter = self.automation.ControlViewCondition
        self.max_depth = max_depth

    def tree(self) -> AccessibilityNode:
        root = self.automation.GetRootElement().BuildUpdatedCache(self.cache)
        return uia_node(CachedElement(root), max_depth=self.max_depth)


def running_excel() -> Any | None:
    """The Excel the session's user runs, or None while none runs."""
    try:
        return win32com.client.GetActiveObject("Excel.Application")
    except pywintypes.com_error:
        return None


def make_dpi_aware() -> None:
    ctypes.windll.shcore.SetProcessDpiAwareness(PER_MONITOR_DPI_AWARE)


def screen_bounds() -> Bounds:
    user32 = ctypes.windll.user32
    return Bounds(x=0, y=0, width=user32.GetSystemMetrics(SCREEN_WIDTH_METRIC),
                  height=user32.GetSystemMetrics(SCREEN_HEIGHT_METRIC))
