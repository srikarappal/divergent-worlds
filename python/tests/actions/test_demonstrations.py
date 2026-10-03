from rlworld.actions.demonstrations import InputEvent, demonstration_actions, keysym_character, thinned
from rlworld.actions.models import Point, action_payload

CONTROL_L, SHIFT_L, SUPER_L, RETURN = 0xFFE3, 0xFFE1, 0xFFEB, 0xFF0D


def pointer(at_ms, x, y, buttons=0):
    return InputEvent(at_ms=at_ms, kind="pointer", x=x, y=y, buttons=buttons)


def key(at_ms, keysym, down=True):
    return InputEvent(at_ms=at_ms, kind="key", keysym=keysym, down=down)


def tap(at_ms, keysym):
    return [key(at_ms, keysym), key(at_ms + 5, keysym, down=False)]


def payloads(events):
    return [action_payload(action) for action in demonstration_actions(events)]


def test_presses_become_clicks_double_clicks_and_drags():
    events = [pointer(0, 100, 200), pointer(10, 100, 200, 1), pointer(60, 101, 200),
              pointer(100, 100, 200, 1), pointer(150, 100, 200),
              pointer(300, 400, 400, 4), pointer(340, 400, 400),
              pointer(2000, 10, 10, 1), pointer(2050, 60, 40, 1), pointer(2100, 120, 80, 1), pointer(2150, 120, 80)]
    assert payloads(events) == [
        {"name": "click", "x": 100, "y": 200, "button": "left", "count": 2, "modifiers": []},
        {"name": "click", "x": 400, "y": 400, "button": "right", "count": 1, "modifiers": []},
        {"name": "drag", "path": [{"x": 10, "y": 10}, {"x": 60, "y": 40}, {"x": 120, "y": 80}], "button": "left"},
    ]


def test_slow_second_clicks_stay_separate():
    events = [pointer(0, 5, 5, 1), pointer(20, 5, 5), pointer(900, 5, 5, 1), pointer(920, 5, 5)]
    assert [action["count"] for action in payloads(events)] == [1, 1]


def test_wheel_presses_merge_into_scrolls():
    events = [pointer(0, 50, 60, 16), pointer(5, 50, 60), pointer(10, 50, 60, 16), pointer(15, 50, 60),
              pointer(20, 50, 60, 64), pointer(25, 50, 60), pointer(400, 300, 60, 8), pointer(405, 300, 60)]
    assert payloads(events) == [
        {"name": "scroll", "x": 50, "y": 60, "delta_x": 1, "delta_y": 2},
        {"name": "scroll", "x": 300, "y": 60, "delta_x": 0, "delta_y": -1},
    ]


def test_keys_become_typed_text_and_combos():
    events = [key(0, SHIFT_L), *tap(5, ord("H")), key(20, SHIFT_L, down=False), *tap(30, ord("i")),
              *tap(40, ord(" ")), *tap(50, 0x010000E9), *tap(60, RETURN),
              key(100, CONTROL_L), *tap(110, ord("c")), key(130, CONTROL_L, down=False),
              key(200, CONTROL_L), key(201, SHIFT_L), *tap(210, ord("T")), key(220, SHIFT_L, down=False),
              key(221, CONTROL_L, down=False),
              *tap(300, SUPER_L), *tap(400, 0xFFBF)]
    assert payloads(events) == [
        {"name": "type", "text": "Hi é"},
        {"name": "key", "keys": ["enter"]},
        {"name": "key", "keys": ["ctrl", "c"]},
        {"name": "key", "keys": ["ctrl", "shift", "t"]},
        {"name": "key", "keys": ["meta"]},
        {"name": "key", "keys": ["f2"]},
    ]


def test_modifier_clicks_and_hovering():
    events = [key(0, CONTROL_L), pointer(10, 30, 40, 1), pointer(20, 30, 40), key(30, CONTROL_L, down=False),
              pointer(100, 500, 500), pointer(900, 600, 600),
              pointer(1000, 700, 700), pointer(1600, 700, 700, 1), pointer(1650, 700, 700)]
    assert payloads(events) == [
        {"name": "click", "x": 30, "y": 40, "button": "left", "count": 1, "modifiers": ["ctrl"]},
        {"name": "move", "x": 500, "y": 500},
        {"name": "click", "x": 700, "y": 700, "button": "left", "count": 1, "modifiers": []},
    ]


def test_keysym_characters_and_thinned_drags():
    assert keysym_character(ord("a")) == "a" and keysym_character(0xE9) == "é"
    assert keysym_character(0x0100263A) == "☺" and keysym_character(0xFF0D) is None
    path = [Point(x=index, y=index) for index in range(100)]
    kept = thinned(path)
    assert len(kept) == 32 and kept[0] == path[0] and kept[-1] == path[-1]
    assert thinned(path[:5]) == tuple(path[:5])
