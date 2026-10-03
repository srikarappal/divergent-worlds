"""Operator input from a takeover, turned into canonical actions (DESIGN.md sections 6.3 and 6.14).

The relay records raw VNC input: pointer positions with their button masks and
keysyms going down and up. Presses and releases become clicks, double clicks
and drags; wheel buttons become scrolls; a pointer that rests somewhere new
becomes a move; runs of printable keys become typed text; named keys and keys
held with ctrl, alt or meta become key combos. The result is a demonstration in
the same vocabulary policies act in, so it can serve as SFT data or an oracle.
"""

import math
from collections.abc import Sequence
from operator import attrgetter
from typing import Literal

from pydantic import BaseModel, NonNegativeInt

from rlworld.actions.keys import MODIFIER_ORDER
from rlworld.actions.models import (
    ActionModel,
    ClickAction,
    DragAction,
    KeyAction,
    MoveAction,
    Point,
    ScrollAction,
    TypeAction,
)

BUTTONS = {1: "left", 2: "middle", 4: "right"}
WHEEL_STEPS = {8: (0, -1), 16: (0, 1), 32: (-1, 0), 64: (1, 0)}
SAME_SPOT_PIXELS = 3
DOUBLE_CLICK_MS = 400
REST_MS = 500
MAX_DRAG_POINTS = 32
MODIFIER_KEYSYMS = {0xFFE1: "shift", 0xFFE2: "shift", 0xFFE3: "ctrl", 0xFFE4: "ctrl", 0xFFE7: "meta", 0xFFE8: "meta",
                    0xFFE9: "alt", 0xFFEA: "alt", 0xFFEB: "meta", 0xFFEC: "meta"}
NAMED_KEYSYMS = {
    0xFF08: "backspace", 0xFF09: "tab", 0xFF0D: "enter", 0xFF8D: "enter", 0xFF1B: "escape", 0xFFFF: "delete",
    0xFF63: "insert", 0xFF50: "home", 0xFF51: "left", 0xFF52: "up", 0xFF53: "right", 0xFF54: "down",
    0xFF55: "page_up", 0xFF56: "page_down", 0xFF57: "end", 0xFFE5: "caps_lock", 0xFF61: "print_screen",
    **{0xFFBE + index: f"f{index + 1}" for index in range(24)},
}
UNICODE_KEYSYM_OFFSET = 0x01000000


class InputEvent(BaseModel):
    at_ms: int
    kind: Literal["pointer", "key"]
    x: NonNegativeInt = 0
    y: NonNegativeInt = 0
    buttons: NonNegativeInt = 0
    keysym: NonNegativeInt = 0
    down: bool = False


def keysym_character(keysym: int) -> str | None:
    """The character a keysym types: ASCII and Latin 1 keysyms are their code points, Unicode ones are offset."""
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)
    if UNICODE_KEYSYM_OFFSET < keysym <= UNICODE_KEYSYM_OFFSET + 0x10FFFF:
        return chr(keysym - UNICODE_KEYSYM_OFFSET)
    return None


def same_spot(first: Point, second: Point) -> bool:
    return abs(first.x - second.x) <= SAME_SPOT_PIXELS and abs(first.y - second.y) <= SAME_SPOT_PIXELS


def thinned(path: Sequence[Point]) -> tuple[Point, ...]:
    """Keeps a drag's first and last point and evenly spaced ones between, at most MAX_DRAG_POINTS in all."""
    if len(path) <= MAX_DRAG_POINTS:
        return tuple(path)
    stride = (len(path) - 1) / (MAX_DRAG_POINTS - 1)
    return tuple(path[math.floor(index * stride)] for index in range(MAX_DRAG_POINTS - 1)) + (path[-1],)


class DemonstrationBuilder:
    """Walks the events in order; each kind of input updates its own piece of state."""

    def __init__(self) -> None:
        self.actions: list[ActionModel] = []
        self.typed = ""
        self.held_modifiers: set[str] = set()
        self.modifier_alone = False
        self.buttons = 0
        self.position = Point(x=0, y=0)
        self.pointer_at_ms: int | None = None
        self.press: tuple[str, int, list[Point]] | None = None
        self.last_click: tuple[ClickAction, int] | None = None
        self.last_action_position: Point | None = None

    def emit(self, action: ActionModel, position: Point | None = None) -> None:
        if self.typed:
            typed, self.typed = self.typed, ""
            self.actions.append(TypeAction(text=typed))
        self.actions.append(action)
        self.last_action_position = position or self.last_action_position
        if not isinstance(action, ClickAction):
            self.last_click = None

    def modifiers(self) -> tuple[str, ...]:
        return tuple(modifier for modifier in MODIFIER_ORDER if modifier in self.held_modifiers)

    def rested(self, event: InputEvent) -> None:
        """A pointer that stayed somewhere new long enough to hover there becomes a move, unless a press follows."""
        if self.pointer_at_ms is None or self.buttons or event.at_ms - self.pointer_at_ms < REST_MS:
            return
        if self.last_action_position is not None and same_spot(self.position, self.last_action_position):
            return
        pressing_here = (event.kind == "pointer" and event.buttons & ~self.buttons
                         and same_spot(Point(x=event.x, y=event.y), self.position))
        if not pressing_here:
            self.emit(MoveAction(x=self.position.x, y=self.position.y), self.position)

    def pointer(self, event: InputEvent) -> None:
        position = Point(x=event.x, y=event.y)
        pressed, released = event.buttons & ~self.buttons, self.buttons & ~event.buttons
        if self.press is not None and position != self.press[2][-1]:
            self.press[2].append(position)
        for mask, (delta_x, delta_y) in WHEEL_STEPS.items():
            if pressed & mask:
                self.scroll(position, delta_x, delta_y)
        for mask, button in BUTTONS.items():
            if pressed & mask and self.press is None:
                self.press = (button, event.at_ms, [position])
            if released & mask and self.press is not None and self.press[0] == button:
                self.release(position, event.at_ms)
        self.buttons, self.position, self.pointer_at_ms = event.buttons, position, event.at_ms

    def scroll(self, position: Point, delta_x: int, delta_y: int) -> None:
        last = self.actions[-1] if self.actions and not self.typed else None
        if isinstance(last, ScrollAction) and same_spot(Point(x=last.x, y=last.y), position):
            self.actions[-1] = last.model_copy(update={"delta_x": last.delta_x + delta_x,
                                                       "delta_y": last.delta_y + delta_y})
            return
        self.emit(ScrollAction(x=position.x, y=position.y, delta_x=delta_x, delta_y=delta_y), position)

    def release(self, position: Point, at_ms: int) -> None:
        button, _, path = self.press
        self.press = None
        start = path[0]
        if not same_spot(start, position):
            self.emit(DragAction(path=thinned(path), button=button), position)
            return
        if self.last_click is not None:
            click, clicked_at_ms = self.last_click
            if (click.button == button and click.count < 3 and at_ms - clicked_at_ms <= DOUBLE_CLICK_MS
                    and same_spot(Point(x=click.x, y=click.y), start) and self.actions[-1] is click):
                repeated = click.model_copy(update={"count": click.count + 1})
                self.actions[-1] = repeated
                self.last_click = (repeated, at_ms)
                return
        click = ClickAction(x=start.x, y=start.y, button=button, modifiers=self.modifiers())
        self.emit(click, start)
        self.last_click = (click, at_ms)
        self.modifier_alone = False

    def key(self, event: InputEvent) -> None:
        if event.keysym in MODIFIER_KEYSYMS:
            modifier = MODIFIER_KEYSYMS[event.keysym]
            if event.down:
                self.held_modifiers.add(modifier)
                self.modifier_alone = modifier != "shift"
            else:
                if self.modifier_alone and modifier in self.held_modifiers:
                    self.emit(KeyAction(keys=self.modifiers()))
                self.held_modifiers.discard(modifier)
                self.modifier_alone = False
            return
        if not event.down:
            return
        self.modifier_alone = False
        character = keysym_character(event.keysym)
        combo_modifiers = self.held_modifiers - {"shift"}
        if character is not None and not combo_modifiers:
            self.typed += character
            return
        name = NAMED_KEYSYMS.get(event.keysym)
        if name is None and character is not None:
            name = "space" if character == " " else character
        if name is not None:
            self.emit(KeyAction(keys=(*self.modifiers(), name)))

    def build(self, events: Sequence[InputEvent]) -> list[ActionModel]:
        for event in sorted(events, key=attrgetter("at_ms")):
            self.rested(event)
            if event.kind == "pointer":
                self.pointer(event)
            else:
                self.key(event)
        if self.typed:
            self.actions.append(TypeAction(text=self.typed))
            self.typed = ""
        return self.actions


def demonstration_actions(events: Sequence[InputEvent]) -> list[ActionModel]:
    return DemonstrationBuilder().build(events)
