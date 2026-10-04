import pytest

from rlworld.actions.keys import (
    KeyNameError,
    normalize_key,
    normalize_key_combo,
    render_playwright_combo,
    render_text_combo,
    render_typed_keys,
    render_xdotool_combo,
    split_key_combo,
)


@pytest.mark.parametrize(
    ("raw_key", "canonical_key"),
    [
        ("Control", "ctrl"),
        ("cmd", "meta"),
        ("Super_L", "meta"),
        ("win", "meta"),
        ("option", "alt"),
        ("Return", "enter"),
        ("KP_Enter", "enter"),
        ("ESC", "escape"),
        ("BackSpace", "backspace"),
        ("Page_Up", "page_up"),
        ("page down", "page_down"),
        ("Prior", "page_up"),
        ("ArrowLeft", "left"),
        ("F11", "f11"),
        ("A", "a"),
        (" ", "space"),
        ("plus", "+"),
        ("/", "/"),
    ],
)
def test_aliases_map_to_canonical_names(raw_key, canonical_key):
    assert normalize_key(raw_key) == canonical_key


def test_unknown_keys_are_rejected():
    with pytest.raises(KeyNameError):
        normalize_key("hyperspace")


@pytest.mark.parametrize(
    ("combo_text", "raw_keys"),
    [
        ("ctrl+shift+s", ["ctrl", "shift", "s"]),
        ("ctrl s", ["ctrl", "s"]),
        ("ctrl++", ["ctrl", "+"]),
        ("+", ["+"]),
        (" ", [" "]),
        ("  Return ", ["Return"]),
    ],
)
def test_split_key_combo(combo_text, raw_keys):
    assert split_key_combo(combo_text) == raw_keys


def test_combos_put_modifiers_first_and_drop_repeats():
    assert normalize_key_combo("s+shift+ctrl+s") == ("ctrl", "shift", "s")
    assert normalize_key_combo(["META", "alt", "Tab"]) == ("alt", "meta", "tab")


def test_empty_combos_are_rejected():
    with pytest.raises(KeyNameError):
        normalize_key_combo([])


def test_renderers():
    keys = ("ctrl", "shift", "page_down")
    assert render_xdotool_combo(keys) == "ctrl+shift+Page_Down"
    assert render_typed_keys(keys) == ["CTRL", "SHIFT", "PAGEDOWN"]
    assert render_text_combo(keys) == "ctrl shift pagedown"
    assert render_xdotool_combo(("meta", "f4")) == "super+F4"
    assert render_typed_keys(("left", "a")) == ["ARROWLEFT", "A"]
    assert render_playwright_combo(keys) == "Control+Shift+PageDown"
    assert render_playwright_combo(("meta", "up", "space", "f12", "a")) == "Meta+ArrowUp+Space+F12+a"


def test_rendered_combos_normalize_back():
    for keys in [("ctrl", "+"), ("alt", "f4"), ("meta", "escape"), ("shift", "-")]:
        assert normalize_key_combo(render_xdotool_combo(keys)) == keys
        assert normalize_key_combo(render_typed_keys(keys)) == keys
        assert normalize_key_combo(render_playwright_combo(keys)) == keys
