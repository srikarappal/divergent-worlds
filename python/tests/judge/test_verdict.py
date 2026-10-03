import json

import pytest

from rlworld.judge import JudgeVerdictError, parse_rubric, parse_verdict

RUBRIC = parse_rubric(
    "---\nid: tone\nversion: v1\nitems:\n  - {id: polite, question: q1}\n  - {id: brief, question: q2}\n---\n"
)


def reply(items) -> str:
    return json.dumps({"items": items})


def item(item_id, passed=True, confidence=0.9):
    return {"id": item_id, "passed": passed, "evidence": f"{item_id} evidence", "confidence": confidence}


def test_plain_json_parses():
    verdict = parse_verdict(reply([item("polite"), item("brief", passed=False)]), RUBRIC)
    assert verdict.by_item()["brief"].passed is False
    assert verdict.by_item()["polite"].confidence == 0.9


def test_fenced_json_inside_prose_parses():
    text = "Here is my grading.\n```json\n" + reply([item("polite"), item("brief")]) + "\n```\nThanks."
    assert len(parse_verdict(text, RUBRIC).items) == 2


@pytest.mark.parametrize(
    ("response_text", "fragment"),
    [
        (reply([item("polite")]), "missing items ['brief']"),
        (reply([item("polite"), item("brief"), item("bonus")]), "unknown items ['bonus']"),
        (reply([item("polite"), item("polite"), item("brief")]), "duplicate items ['polite']"),
        (reply([{**item("polite"), "passed": "yes"}, item("brief")]), "malformed"),
        (reply([item("polite", confidence=1.5), item("brief")]), "malformed"),
        ("I think it passed.", "no JSON object"),
        ("{not json}", "malformed"),
    ],
)
def test_bad_verdicts_are_rejected(response_text, fragment):
    with pytest.raises(JudgeVerdictError) as raised:
        parse_verdict(response_text, RUBRIC)
    assert fragment in str(raised.value)
