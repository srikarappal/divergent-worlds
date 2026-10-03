import pytest

from rlworld.rewards.diff import diff_states, path_matches, side_effect_violations


def change_summary(before, after):
    return [(change.path, change.kind) for change in diff_states(before, after)]


def test_nested_dict_changes():
    before = {"crm": {"deals": {"d1": {"stage": "open", "amount": 10}}}}
    after = {"crm": {"deals": {"d1": {"stage": "won", "amount": 10}, "d2": {"stage": "open"}}}}
    assert change_summary(before, after) == [("crm/deals/d1/stage", "modified"), ("crm/deals/d2", "added")]


def test_lists_of_records_diff_by_id():
    before = {"mail": {"sent": [{"id": "s1", "body": "draft"}]}}
    after = {"mail": {"sent": [{"id": "s2", "body": "new"}, {"id": "s1", "body": "final"}]}}
    assert change_summary(before, after) == [("mail/sent/s1/body", "modified"), ("mail/sent/s2", "added")]


def test_plain_lists_diff_by_position():
    assert change_summary({"values": [1, 2, 3]}, {"values": [1, 5]}) == [
        ("values/1", "modified"),
        ("values/2", "removed"),
    ]


def test_duplicate_ids_fall_back_to_positions():
    before = {"rows": [{"id": 1, "v": "a"}, {"id": 1, "v": "b"}]}
    after = {"rows": [{"id": 1, "v": "a"}, {"id": 1, "v": "c"}]}
    assert change_summary(before, after) == [("rows/1/v", "modified")]


def test_an_added_subtree_is_reported_once():
    changes = diff_states({}, {"files": {"a.txt": {"size": 3}, "b.txt": {"size": 4}}})
    assert len(changes) == 1
    assert (changes[0].path, changes[0].kind) == ("files", "added")
    assert changes[0].after == {"a.txt": {"size": 3}, "b.txt": {"size": 4}}


def test_file_names_keep_their_dots():
    assert change_summary({"files": {"report.v2.xlsx": 1}}, {"files": {"report.v2.xlsx": 2}}) == [
        ("files/report.v2.xlsx", "modified")
    ]


def test_equal_states_have_no_changes():
    state = {"mail": {"sent": [{"id": "s1"}]}, "files": {"a": [1, {"b": None}]}}
    assert diff_states(state, state) == []


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("files/reconciliation.xlsx", "files/reconciliation.xlsx", True),
        ("files/reconciliation.xlsx/sheets/Summary", "files/reconciliation.xlsx", True),
        ("files/invoices.csv", "files/reconciliation.xlsx", False),
        ("files/q3.xlsx", "files/*.xlsx", True),
        ("mail/sent/s1/body", "mail/sent", True),
        ("mail", "mail/sent", False),
        ("crm/deals/d7/stage", "crm/**/stage", True),
        ("crm/stage", "crm/**/stage", True),
        ("crm/deals/d7/amount", "crm/**/stage", False),
        ("anything/at/all", "**", True),
    ],
)
def test_path_matches(path, pattern, expected):
    assert path_matches(path, pattern) is expected


def test_side_effect_violations_skip_allowed_and_volatile_paths():
    before = {"files": {"report.xlsx": 1, "invoices.csv": 1}, "browser": {"history": []}, "mail": {"sent": []}}
    after = {
        "files": {"report.xlsx": 2},
        "browser": {"history": [{"id": "h1", "url": "https://crm.example.com"}]},
        "mail": {"sent": [{"id": "s1"}]},
    }
    changes = diff_states(before, after)
    violations = side_effect_violations(changes, ["files/report.xlsx", "mail/sent"], ["browser/history"])
    assert [violation.path for violation in violations] == ["files/invoices.csv"]
    assert [violation.path for violation in side_effect_violations(changes, ["**"])] == []
