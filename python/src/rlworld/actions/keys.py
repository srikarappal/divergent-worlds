"""Key names: one canonical vocabulary plus renderers for each model format."""

import re


class KeyNameError(ValueError):
    """A key name that maps to nothing in the canonical vocabulary."""


MODIFIER_ORDER = ("ctrl", "alt", "shift", "meta")
FUNCTION_KEYS = tuple(f"f{number}" for number in range(1, 25))
NAMED_KEYS = frozenset(
    {
        *MODIFIER_ORDER,
        "enter",
        "tab",
        "escape",
        "backspace",
        "delete",
        "insert",
        "space",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "page_up",
        "page_down",
        "caps_lock",
        "print_screen",
        *FUNCTION_KEYS,
    }
)
KEY_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "cmd": "meta",
    "command": "meta",
    "super": "meta",
    "super_l": "meta",
    "super_r": "meta",
    "win": "meta",
    "windows": "meta",
    "meta_l": "meta",
    "meta_r": "meta",
    "option": "alt",
    "opt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
    "return": "enter",
    "kp_enter": "enter",
    "esc": "escape",
    "del": "delete",
    "ins": "insert",
    "bksp": "backspace",
    "back_space": "backspace",
    "pgup": "page_up",
    "pageup": "page_up",
    "prior": "page_up",
    "pgdn": "page_down",
    "pagedown": "page_down",
    "next": "page_down",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "spacebar": "space",
    "capslock": "caps_lock",
    "printscreen": "print_screen",
    "print": "print_screen",
    "plus": "+",
    "minus": "-",
}
XDOTOOL_NAMES = {
    "meta": "super",
    "enter": "Return",
    "tab": "Tab",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "insert": "Insert",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "page_up": "Page_Up",
    "page_down": "Page_Down",
    "caps_lock": "Caps_Lock",
    "print_screen": "Print",
    "+": "plus",
    "-": "minus",
    **{function_key: function_key.upper() for function_key in FUNCTION_KEYS},
}
TYPED_NAMES = {
    "escape": "ESC",
    "up": "ARROWUP",
    "down": "ARROWDOWN",
    "left": "ARROWLEFT",
    "right": "ARROWRIGHT",
    "page_up": "PAGEUP",
    "page_down": "PAGEDOWN",
    "caps_lock": "CAPSLOCK",
    "print_screen": "PRINTSCREEN",
}
TEXT_NAMES = {
    "escape": "esc",
    "page_up": "pageup",
    "page_down": "pagedown",
    "caps_lock": "capslock",
    "print_screen": "printscreen",
}


def normalize_key(raw_key: str) -> str:
    if raw_key == " ":
        return "space"
    if len(raw_key) == 1:
        return raw_key.lower()
    lowered_key = re.sub(r"[\s\-]+", "_", raw_key.strip().lower())
    canonical_key = KEY_ALIASES.get(lowered_key, lowered_key)
    if canonical_key in NAMED_KEYS or len(canonical_key) == 1:
        return canonical_key
    raise KeyNameError(f"unknown key {raw_key!r}")


def split_key_combo(combo_text: str) -> list[str]:
    """Split "ctrl+shift+s", "ctrl s" or "ctrl++" into raw key names."""
    if combo_text == " ":
        return [" "]
    stripped_text = combo_text.strip()
    if stripped_text == "+":
        return ["+"]
    if stripped_text.endswith("++"):
        return [part for part in stripped_text[:-2].split("+") if part] + ["+"]
    separator_pattern = r"\+" if "+" in stripped_text else r"\s+"
    return [part for part in re.split(separator_pattern, stripped_text) if part]


def normalize_key_combo(combo: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Canonical combo: modifiers first in a fixed order, then the other keys once each."""
    raw_keys = split_key_combo(combo) if isinstance(combo, str) else list(combo)
    if not raw_keys:
        raise KeyNameError("a key combo needs at least one key")
    normalized_keys = [normalize_key(raw_key) for raw_key in raw_keys]
    modifiers = [modifier for modifier in MODIFIER_ORDER if modifier in normalized_keys]
    other_keys = list(dict.fromkeys(key for key in normalized_keys if key not in MODIFIER_ORDER))
    return tuple(modifiers + other_keys)


def render_xdotool_combo(keys: tuple[str, ...]) -> str:
    return "+".join(XDOTOOL_NAMES.get(key, key) for key in keys)


def render_typed_keys(keys: tuple[str, ...]) -> list[str]:
    return [TYPED_NAMES.get(key, key.upper()) for key in keys]


def render_text_combo(keys: tuple[str, ...]) -> str:
    return " ".join(TEXT_NAMES.get(key, key) for key in keys)
