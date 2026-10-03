import pytest
from pydantic import ValidationError

from rlworld.actions.models import (
    MODALITIES,
    ActionNotAllowedError,
    ClickAction,
    DoneAction,
    ExecAction,
    Observation,
    action_classes_for,
    action_name,
    action_payload,
    ensure_actions_allowed,
    parse_action,
)


def test_parse_dispatches_on_the_action_name():
    action = parse_action({"name": "click", "x": 3, "y": 4, "modifiers": "Control+shift"})
    assert isinstance(action, ClickAction)
    assert action.modifiers == ("ctrl", "shift")
    assert action_payload(action) == {
        "name": "click",
        "x": 3,
        "y": 4,
        "button": "left",
        "count": 1,
        "modifiers": ["ctrl", "shift"],
    }


def test_every_modality_has_actions():
    assert {action_class.modality for action_class in action_classes_for(MODALITIES)} == set(MODALITIES)
    assert [action_name(action_class) for action_class in action_classes_for(["terminal"])] == ["exec"]


def test_episode_ending_actions():
    ending_names = {action_name(action_class) for action_class in action_classes_for(MODALITIES)
                    if action_class.ends_episode}
    assert ending_names == {"answer", "done", "give_up"}


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"name": "click", "x": -1, "y": 4},
        {"name": "click", "x": 1, "y": 4, "count": 4},
        {"name": "click", "x": 1, "y": 4, "modifiers": ["ctrl", "a"]},
        {"name": "drag", "path": [{"x": 1, "y": 1}]},
        {"name": "scroll", "x": 1, "y": 1},
        {"name": "type", "text": ""},
        {"name": "wait", "duration_ms": 120000},
        {"name": "tab", "operation": "switch"},
        {"name": "tab", "operation": "close", "url": "https://example.com"},
        {"name": "exec", "command": "ls", "timeout_s": 0},
        {"name": "teleport"},
        {"name": "done", "unexpected": True},
    ],
)
def test_invalid_actions_are_rejected(invalid_payload):
    with pytest.raises(ValidationError):
        parse_action(invalid_payload)


def test_actions_are_immutable():
    action = parse_action({"name": "done"})
    with pytest.raises(ValidationError):
        action.reason = "changed"


def test_disallowed_modalities_are_rejected():
    actions = [DoneAction(), ExecAction(command="rm -rf /tmp/scratch")]
    ensure_actions_allowed(actions, ["control", "terminal"])
    with pytest.raises(ActionNotAllowedError) as raised:
        ensure_actions_allowed(actions, ["computer", "control"])
    assert "exec" in str(raised.value)


def test_observation_carries_any_mix_of_parts():
    observation = Observation.model_validate(
        {
            "step_index": 3,
            "screenshot": {"uri": "s3://frames/3.webp", "width": 1280, "height": 800, "sha256": "ab"},
            "terminal": {"stdout": "ok\n", "exit_code": 0},
            "file_changes": [{"path": "files/reconciliation.xlsx", "change": "modified"}],
            "user_reply": "Use Q3",
        }
    )
    assert observation.screenshot.image_format == "webp"
    assert observation.file_changes[0].change == "modified"
