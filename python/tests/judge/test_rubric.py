import re

import pytest

from rlworld.judge import RubricError, RubricLibrary, parse_rubric

VALID_RUBRIC = """---
id: tone_check
version: v1
items:
  - id: polite
    question: Is the reply polite?
---
Judge the reply only.
"""


def test_the_example_rubric_loads(rubric_library):
    rubric = rubric_library.load("rubrics/summary_email_quality.md")
    assert (rubric.id, rubric.version) == ("summary_email_quality", "v3")
    assert [item.id for item in rubric.items] == [
        "names_every_discrepancy",
        "states_the_reconciled_total",
        "professional_and_concise",
    ]
    assert rubric.items[2].weight == 0.5
    assert rubric.guidance.startswith("Grade the summary email")
    assert re.fullmatch(r"v3\+[0-9a-f]{12}", rubric.pinned_version)


def test_editing_the_text_changes_the_pinned_version():
    original = parse_rubric(VALID_RUBRIC)
    edited = parse_rubric(VALID_RUBRIC.replace("Judge the reply only.", "Judge the reply and its subject."))
    assert original.version == edited.version
    assert original.pinned_version != edited.pinned_version


@pytest.mark.parametrize(
    ("rubric_text", "fragment"),
    [
        ("no front matter", "front matter"),
        ("---\nid: a\nversion: v1\n", "not closed"),
        ("---\n- a list\n---\n", "must be a mapping"),
        ("---\nid: a\nversion: v1\nitems: []\n---\n", "items"),
        (
            "---\nid: a\nversion: v1\nitems:\n  - {id: x, question: q}\n  - {id: x, question: r}\n---\n",
            "unique",
        ),
        ("---\nid: Bad Id\nversion: v1\nitems:\n  - {id: x, question: q}\n---\n", "id"),
    ],
)
def test_malformed_rubrics_are_rejected(rubric_text, fragment):
    with pytest.raises(RubricError) as raised:
        parse_rubric(rubric_text)
    assert fragment in str(raised.value)


def test_the_library_stays_inside_its_root(tmp_path):
    (tmp_path / "rubrics").mkdir()
    (tmp_path / "rubrics" / "tone.md").write_text(VALID_RUBRIC)
    library = RubricLibrary(tmp_path / "rubrics")
    assert library.load("tone.md") is library.load("tone.md")
    with pytest.raises(RubricError) as escaped:
        library.load("../../etc/passwd")
    assert "escapes" in str(escaped.value)
    with pytest.raises(RubricError) as missing:
        library.load("absent.md")
    assert "does not exist" in str(missing.value)
