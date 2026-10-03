"""Golden tests: every adapter against recorded model outputs and expected renders."""

import json
from pathlib import Path
from typing import Any

import pytest

from rlworld.actions.adapters import ActionParseError, ActionRenderError, get_adapter
from rlworld.actions.models import parse_action

GOLDEN_DIRECTORY = Path(__file__).resolve().parent / "golden"
GOLDEN_SUITES = [json.loads(golden_path.read_text()) for golden_path in sorted(GOLDEN_DIRECTORY.glob("*.json"))]


def golden_cases(section_name: str) -> list[Any]:
    cases = []
    for suite in GOLDEN_SUITES:
        for case_index, case in enumerate(suite[section_name]):
            case_label = case.get("description", f"case {case_index}")
            cases.append(pytest.param(suite, case, id=f"{suite['adapter']}: {case_label}"))
    return cases


def adapter_and_geometry(suite: dict[str, Any], case: dict[str, Any]):
    adapter = get_adapter(suite["adapter"], **{**suite["options"], **case.get("options", {})})
    env_width, env_height = case["env_size"]
    return adapter, adapter.model_geometry(env_width, env_height)


def test_every_adapter_has_a_golden_suite():
    assert {suite["adapter"] for suite in GOLDEN_SUITES} == {"computer_tool", "typed_actions", "text_actions"}


@pytest.mark.parametrize(("suite", "case"), golden_cases("parse_cases"))
def test_parse(suite, case):
    adapter, geometry = adapter_and_geometry(suite, case)
    expected_actions = [parse_action(expected_action) for expected_action in case["expected_actions"]]
    assert adapter.parse_output(case["raw_output"], geometry) == expected_actions


@pytest.mark.parametrize(("suite", "case"), golden_cases("parse_error_cases"))
def test_parse_errors(suite, case):
    adapter, geometry = adapter_and_geometry(suite, case)
    with pytest.raises(ActionParseError) as raised:
        adapter.parse_output(case["raw_output"], geometry)
    assert case["error_fragment"] in str(raised.value)


@pytest.mark.parametrize(("suite", "case"), golden_cases("render_cases"))
def test_render(suite, case):
    adapter, geometry = adapter_and_geometry(suite, case)
    assert adapter.render_action(parse_action(case["action"]), geometry) == case["expected_rendered"]


@pytest.mark.parametrize(("suite", "case"), golden_cases("render_error_cases"))
def test_render_errors(suite, case):
    adapter, geometry = adapter_and_geometry(suite, case)
    with pytest.raises(ActionRenderError) as raised:
        adapter.render_action(parse_action(case["action"]), geometry)
    assert case["error_fragment"] in str(raised.value)
