import base64
import hashlib
import json

import pytest

from rlworld.rewards.verifiers import VerifierContext
from rlworld.specs.models import TaskSpec
from rlworld.tasks.osworld_evaluation import (
    METRICS,
    Evaluation,
    Getter,
    MetricCall,
    check_include_exclude,
    check_json_keybindings,
    check_json_settings,
    compare_text_file,
    evaluation_checks,
    exact_match,
    getter_value,
    is_in_list,
    literal_match,
    match_in_list,
    summary,
)

TASK = TaskSpec(generator="tasks.public.osworld", allowed_actions=["computer", "control"], observation=["screenshot"],
                verifiers={"exact": ["verifiers.public.osworld"]}, oracle="oracles.public.osworld", split="eval")
EXPECTED_NOTES = b"Meeting at 10\nBring the budget\n"


class AssetShelf:
    def __init__(self, assets):
        self.assets = assets
        self.reads = []

    def read(self, digest):
        self.reads.append(digest)
        return self.assets[digest]


def encoded_file(content):
    return {"base64": base64.b64encode(content).decode("ascii"), "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def reads(files=None, commands=None):
    return {"files": {path: encoded_file(content) for path, content in (files or {}).items()},
            "commands": {command: {"stdout": stdout, "stderr": f"warning from {command}", "exit_code": 0}
                         for command, stdout in (commands or {}).items()}}


def context(task_reads=None, final_action="done"):
    final_state = {} if task_reads is None else {"reads": task_reads}
    return VerifierContext(task=TASK, initial_state={}, final_state=final_state, changes=(), ground_truth={},
                           final_action=final_action)


def rule(rules):
    return Getter(kind="rule", rules=rules)


def command(text):
    return Getter(kind="vm_command_line", command=text)


def test_exact_match_and_lists_follow_osworld():
    assert exact_match("1\n", {"expected": "1\n"}, {}) == 1.0
    assert exact_match("1", {"expected": "1\n"}, {}) == 0.0
    assert match_in_list("Bing", {"expected": ["Microsoft Bing", "Bing"]}, {}) == 1.0
    assert match_in_list(None, {"expected": ["Bing"]}, {}) == 0.0
    assert is_in_list(["vim", "code"], {"expected": "code"}, {}) == 1.0
    assert is_in_list("installed: code", {"expected": "code"}, {}) == 1.0
    assert is_in_list("installed: vim", {"expected": "code"}, {}) == 0.0
    assert is_in_list(None, {"expected": "code"}, {}) == 0.0


def test_include_exclude_needs_every_include_and_no_exclude():
    rules = {"include": ["spotify"], "exclude": ["not found"]}
    assert check_include_exclude("/snap/bin/spotify", rules, {}) == 1.0
    assert check_include_exclude("spotify not found", rules, {}) == 0.0
    assert check_include_exclude("", rules, {}) == 0.0
    assert check_include_exclude(None, rules, {}) == 0.0
    assert check_include_exclude("anything", {}, {}) == 1.0


def test_literal_match_compares_text_or_lists():
    assert literal_match("Arial", "Arial", {}) == 1.0
    assert literal_match("arial", "Arial", {}) == 0.0
    assert literal_match("arial", "Arial", {"ignore_case": True}) == 1.0
    assert literal_match(12, "12", {}) == 1.0
    assert literal_match(["A", 2], ["a", "2"], {"type": "list", "ignore_case": True}) == 1.0
    assert literal_match(["A"], ["A", "B"], {"type": "list"}) == 0.0
    assert literal_match("A", ["A"], {"type": "list"}) == 0.0
    with pytest.raises(ValueError, match="no type"):
        literal_match("A", "A", {"type": "number"})


def test_text_files_compare_with_optional_blank_and_case_folding():
    assert compare_text_file(EXPECTED_NOTES, EXPECTED_NOTES, {}) == 1.0
    assert compare_text_file(b"Meeting at 10\n\tBring the budget", EXPECTED_NOTES, {}) == 0.0
    assert compare_text_file(b"Meeting  at 10\n\tBring the budget", EXPECTED_NOTES, {"ignore_blanks": True}) == 1.0
    assert compare_text_file(b"MEETING AT 10\nBRING THE BUDGET\n", EXPECTED_NOTES, {"ignore_case": True}) == 1.0
    assert compare_text_file(b"", b"", {}) == 1.0
    assert compare_text_file(b"\xff\xfe binary", EXPECTED_NOTES, {}) == 0.0
    assert compare_text_file(None, EXPECTED_NOTES, {}) == 0.0


def test_json_settings_need_every_expected_value():
    settings = json.dumps({"editor.fontSize": 14, "files.autoSave": "afterDelay"}).encode()
    assert check_json_settings(settings, {"expected": {"editor.fontSize": 14}}, {}) == 1.0
    assert check_json_settings(settings, {"expected": {"editor.fontSize": 16}}, {}) == 0.0
    assert check_json_settings(settings, {"expected": {"editor.tabSize": 4}}, {}) == 0.0
    assert check_json_settings(b"// comments make this invalid\n{}", {"expected": {}}, {}) == 0.0
    assert check_json_settings(b"[1, 2]", {"expected": {}}, {}) == 0.0
    assert check_json_settings(None, {"expected": {}}, {}) == 0.0


def test_keybindings_may_sit_under_a_comment_line():
    binding = {"key": "ctrl+j", "command": "workbench.action.togglePanel"}
    with_comment = b"// Place your key bindings in this file\n" + json.dumps([binding]).encode()
    assert check_json_keybindings(with_comment, {"expected": binding}, {}) == 1.0
    assert check_json_keybindings(json.dumps([binding]).encode(), {"expected": binding}, {}) == 1.0
    assert check_json_keybindings(b"[]", {"expected": binding}, {}) == 0.0
    assert check_json_keybindings(json.dumps(binding).encode(), {"expected": binding}, {}) == 0.0
    assert check_json_keybindings(b"not json at all", {"expected": binding}, {}) == 0.0
    assert check_json_keybindings(None, {"expected": binding}, {}) == 0.0


def test_the_supported_metrics_are_exactly_these():
    assert set(METRICS) == {"exact_match", "match_in_list", "is_in_list", "check_include_exclude", "literal_match",
                            "compare_text_file", "check_json_settings", "check_json_keybindings"}


def test_getters_read_files_commands_rules_and_assets():
    shelf = AssetShelf({"d1": b"one", "d2": b"two"})
    task_reads = reads({"Desktop/a.txt": b"A", "Desktop/b.txt": b"B"}, {"echo hi": "hi\n"})
    assert getter_value(Getter(kind="vm_file", paths=("Desktop/a.txt",)), task_reads, shelf) == b"A"
    assert getter_value(Getter(kind="vm_file", paths=("Desktop/a.txt", "Desktop/b.txt")), task_reads,
                        shelf) == [b"A", b"B"]
    assert getter_value(Getter(kind="vm_file", paths=("Desktop/gone.txt",)), task_reads, shelf) is None
    missing = {"files": {"Desktop/a.txt": {"missing": True, "error": "No such file"}}}
    assert getter_value(Getter(kind="vm_file", paths=("Desktop/a.txt",)), missing, shelf) is None
    assert getter_value(command("echo hi"), task_reads, shelf) == "hi\n"
    assert getter_value(Getter(kind="vm_command_error", command="echo hi"), task_reads, shelf) == (
        "warning from echo hi")
    assert getter_value(command("never ran"), task_reads, shelf) is None
    assert getter_value(rule({"expected": 3}), {}, shelf) == {"expected": 3}
    assert getter_value(Getter(kind="cloud_file", assets=("d2",)), {}, shelf) == b"two"
    assert getter_value(Getter(kind="cloud_file", assets=("d1", "d2")), {}, shelf) == [b"one", b"two"]


def metric(func, result, expected, **options):
    return MetricCall(func=func, result=result, expected=expected, options=options)


NOTES_METRIC = metric("compare_text_file", Getter(kind="vm_file", paths=("Desktop/notes.txt",)),
                      Getter(kind="cloud_file", assets=("notes",)), ignore_blanks=True)
SPOTIFY_METRIC = metric("check_include_exclude", command("which spotify"),
                        rule({"include": ["spotify"], "exclude": ["not found"]}))
BROKEN_METRIC = metric("exact_match", command("which spotify"), rule({"no_expected_key": True}))


def check_outcomes(evaluation, task_reads, final_action="done"):
    checks = evaluation_checks(evaluation, context(task_reads, final_action), AssetShelf({"notes": EXPECTED_NOTES}))
    return [(check.name, check.passed) for check in checks]


def test_and_needs_every_metric_and_stops_at_the_first_failure():
    evaluation = Evaluation(metrics=(SPOTIFY_METRIC, NOTES_METRIC))
    passing = reads({"Desktop/notes.txt": b"Meeting at 10 Bring the budget"}, {"which spotify": "/snap/bin/spotify"})
    assert check_outcomes(evaluation, passing) == [("osworld_check_include_exclude", True),
                                                   ("osworld_compare_text_file", True)]
    failing = reads({}, {"which spotify": "spotify not found"})
    assert check_outcomes(Evaluation(metrics=(SPOTIFY_METRIC, BROKEN_METRIC)), failing) == [
        ("osworld_check_include_exclude", False)]


def test_or_needs_any_metric_and_stops_at_the_first_pass():
    evaluation = Evaluation(conj="or", metrics=(SPOTIFY_METRIC, NOTES_METRIC, BROKEN_METRIC))
    only_notes = reads({"Desktop/notes.txt": EXPECTED_NOTES}, {"which spotify": ""})
    assert check_outcomes(evaluation, only_notes) == [("osworld_compare_text_file", True)]
    nothing = Evaluation(conj="or", metrics=(SPOTIFY_METRIC, NOTES_METRIC))
    checks = evaluation_checks(nothing, context(reads()), AssetShelf({"notes": EXPECTED_NOTES}))
    assert [(check.name, check.passed) for check in checks] == [("osworld_any_metric", False)]
    assert "osworld_check_include_exclude, osworld_compare_text_file" in checks[0].detail


def test_infeasible_tasks_pass_only_when_the_agent_gives_up():
    infeasible = Evaluation(infeasible=True)
    assert check_outcomes(infeasible, None, "give_up") == [("osworld_infeasible", True)]
    assert check_outcomes(infeasible, None, "done") == [("osworld_infeasible", False)]
    assert check_outcomes(infeasible, None, None) == [("osworld_infeasible", False)]
    feasible = Evaluation(metrics=(SPOTIFY_METRIC,))
    assert check_outcomes(feasible, reads({}, {"which spotify": "/snap/bin/spotify"}), "give_up") == [
        ("osworld_feasible", False)]


def test_a_missing_read_scores_zero_rather_than_erroring():
    assert check_outcomes(Evaluation(metrics=(NOTES_METRIC,)), None) == [("osworld_compare_text_file", False)]


def test_checks_carry_small_summaries_of_what_they_compared():
    evaluation = Evaluation(metrics=(NOTES_METRIC,))
    check = evaluation_checks(evaluation, context(reads({"Desktop/notes.txt": b"wrong"})),
                              AssetShelf({"notes": EXPECTED_NOTES}))[0]
    assert not check.passed and check.detail == "compare_text_file scored 0"
    assert check.actual.startswith("5 bytes, sha256 ") and check.expected.startswith(f"{len(EXPECTED_NOTES)} bytes")


def test_summaries_shorten_bytes_and_long_text():
    assert summary(b"abc") == f"3 bytes, sha256 {hashlib.sha256(b'abc').hexdigest()[:16]}"
    assert summary("x" * 500) == "x" * 300 + "..."
    assert summary(["short", b""]) == ["short", f"0 bytes, sha256 {hashlib.sha256(b'').hexdigest()[:16]}"]
    assert summary({"expected": 1}) == {"expected": 1}
