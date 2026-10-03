import copy
import dataclasses
import re

import pytest

from rlworld.rewards import CheckResult, EpisodeOutcome, FunctionVerifier, JudgeOutcome, VerifierRegistry
from rlworld.rewards.checks import cells_equal, entities_mentioned
from rlworld.specs import parse_spec_document
from rlworld.tasks import GateConfig, GateInputs, SolutionAttempt, TaskGateRunner

EXACT_VERIFIER = "verifiers.excel.reconciliation_values_match"
SEMANTIC_VERIFIER = "verifiers.mail.summary_lists_discrepant_invoices"
INITIAL_STATE = {
    "files": {"reconciliation.xlsx": {"cells": {"B2": {"value": None}, "B3": {"value": None}}}, "invoices.csv": {}},
    "mail": {"sent": []},
}
GROUND_TRUTH = {"total": 1250.0, "discrepant_invoices": ["INV-1001", "INV-1007"]}
DONE = EpisodeOutcome(steps_taken=25, final_action="done")


def state_with(cells=None, email_body="Discrepancies: INV-1001 and INV-1007.", delete_csv=False):
    state = copy.deepcopy(INITIAL_STATE)
    state["files"]["reconciliation.xlsx"]["cells"].update(cells or {"B2": {"value": 1250.0}})
    state["mail"]["sent"] = [{"id": "s1", "body": email_body}]
    if delete_csv:
        del state["files"]["invoices.csv"]
    return state


def attempt(name, state, outcome=DONE):
    return SolutionAttempt(name, state, outcome)


def check_total(context):
    cells = context.final_state["files"]["reconciliation.xlsx"]["cells"]
    return [cells_equal("total in B2", cells, {"B2": context.ground_truth["total"]})]


def check_email(context):
    sent_messages = context.final_state["mail"]["sent"]
    body = sent_messages[-1]["body"] if sent_messages else ""
    return [entities_mentioned("email names discrepancies", body, context.ground_truth["discrepant_invoices"])]


def check_email_verbatim(context):
    sent_messages = context.final_state["mail"]["sent"]
    body = sent_messages[-1]["body"] if sent_messages else ""
    expected_body = "Discrepancies: INV-1001 and INV-1007."
    return [CheckResult(name="email verbatim", tier="semantic", passed=body == expected_body)]


def always_pass(context):
    return [CheckResult(name="anything goes", tier="exact", passed=True)]


def always_pass_semantic(context):
    return [CheckResult(name="anything goes", tier="semantic", passed=True)]


def crash(context):
    raise KeyError("Summary")


def registry_with(exact_function, semantic_function):
    verifier_registry = VerifierRegistry()
    verifier_registry.register(FunctionVerifier(EXACT_VERIFIER, "1", "exact", exact_function))
    verifier_registry.register(FunctionVerifier(SEMANTIC_VERIFIER, "1", "semantic", semantic_function))
    return verifier_registry


def good_inputs(**overrides) -> GateInputs:
    inputs = GateInputs(
        initial_state=INITIAL_STATE,
        ground_truth=GROUND_TRUTH,
        oracle_runs=tuple(attempt(f"oracle_run_{index}", state_with()) for index in range(3)),
        alternative_solutions=(
            attempt("reordered_email", state_with(email_body="Please check inv 1007 and INV-1001; both differ.")),
        ),
        near_misses=(
            attempt("total_in_wrong_cell", state_with(cells={"B3": {"value": 1250.0}})),
            attempt("email_misses_one_invoice", state_with(email_body="Discrepancy: INV-1001.")),
        ),
        agent_attempts=(
            attempt("agent_try_0", state_with(cells={"B2": {"value": 1200.0}})),
            attempt("agent_try_1", state_with()),
        ),
    )
    return dataclasses.replace(inputs, **overrides)


@pytest.fixture
def task(example_payload):
    return parse_spec_document(example_payload).task.model_copy(update={"judge_rubric": None})


def test_a_sound_task_passes_every_gate(task):
    report = TaskGateRunner(registry_with(check_total, check_email)).run(task, good_inputs())
    assert report.passed, report.review_reasons
    assert [result.name for result in report.results] == [
        "null_action",
        "oracle",
        "rerun_stability",
        "alternative_solution",
        "near_miss",
        "solvability",
    ]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", report.task_digest)
    assert report.result("solvability").detail == "a strong agent solved 1 of 2 tries"


def test_a_lenient_verifier_fails_null_action_and_near_miss(task):
    report = TaskGateRunner(registry_with(always_pass, always_pass_semantic)).run(task, good_inputs())
    assert not report.passed
    assert report.result("null_action").detail == "the verifiers pass on the untouched start state"
    assert "total_in_wrong_cell" in report.result("near_miss").detail
    assert report.result("oracle").passed


def test_a_strict_verifier_fails_the_alternative_solution_gate(task):
    report = TaskGateRunner(registry_with(check_total, check_email_verbatim)).run(task, good_inputs())
    alternative_result = report.result("alternative_solution")
    assert not alternative_result.passed
    assert "reordered_email (email verbatim: )" in alternative_result.detail


def test_an_oracle_with_side_effects_flags_the_allowed_changes(task):
    inputs = good_inputs(oracle_runs=tuple(attempt(f"oracle_run_{index}", state_with(delete_csv=True))
                                           for index in range(3)))
    oracle_result = TaskGateRunner(registry_with(check_total, check_email)).run(task, inputs).result("oracle")
    assert not oracle_result.passed
    assert "files/invoices.csv" in oracle_result.detail


def test_flaky_oracle_reruns_fail_stability(task):
    flaky_runs = (
        attempt("oracle_run_0", state_with()),
        attempt("oracle_run_1", state_with(cells={"B2": {"value": 1249.0}})),
        attempt("oracle_run_2", state_with()),
    )
    report = TaskGateRunner(registry_with(check_total, check_email)).run(task, good_inputs(oracle_runs=flaky_runs))
    assert report.result("oracle").passed
    stability_result = report.result("rerun_stability")
    assert not stability_result.passed
    assert "oracle_run_1=failure" in stability_result.detail


def test_too_few_oracle_runs(task):
    inputs = good_inputs(oracle_runs=(attempt("oracle_run_0", state_with()),))
    report = TaskGateRunner(registry_with(check_total, check_email)).run(task, inputs)
    assert report.result("rerun_stability").detail == "needs 3 oracle runs, got 1"


def test_missing_inputs_explain_what_goes_unseen(task):
    inputs = good_inputs(alternative_solutions=(), near_misses=(), agent_attempts=(), oracle_runs=())
    report = TaskGateRunner(registry_with(check_total, check_email)).run(task, inputs)
    assert report.review_reasons == [
        "oracle: no oracle run was provided",
        "rerun_stability: needs 3 oracle runs, got 0",
        "alternative_solution: no alternative valid solution was provided, so false negatives go unseen",
        "near_miss: no near miss was provided, so false positives go unseen",
        "solvability: no strong agent attempts were provided",
    ]


def test_an_unsolvable_task_fails_solvability(task):
    failed_tries = tuple(attempt(f"agent_try_{index}", state_with(cells={"B2": {"value": 1.0}})) for index in range(3))
    report = TaskGateRunner(registry_with(check_total, check_email)).run(task, good_inputs(agent_attempts=failed_tries))
    assert report.result("solvability").detail == "a strong agent solved 0 of 3 tries"
    assert not report.passed


def test_only_the_first_k_tries_count(task):
    tries = (attempt("agent_try_0", state_with(cells={"B2": {"value": 1.0}})), attempt("agent_try_1", state_with()))
    runner = TaskGateRunner(registry_with(check_total, check_email), config=GateConfig(solvability_tries=1))
    assert not runner.run(task, good_inputs(agent_attempts=tries)).result("solvability").passed


def test_a_crashing_verifier_fails_the_gates(task):
    report = TaskGateRunner(registry_with(crash, check_email)).run(task, good_inputs())
    assert report.result("null_action").detail.startswith("evaluation errored: verifier")
    assert not report.result("oracle").passed
    assert "evaluation errored" in report.result("near_miss").detail


class EchoJudge:
    def __init__(self):
        self.calls = 0

    def judge(self, context, prior_checks):
        self.calls += 1
        return JudgeOutcome((CheckResult(name="tone", tier="judge", passed=True),), "judge_model", "v1")


def test_gates_use_the_judge_for_rubric_tasks(example_payload):
    rubric_task = parse_spec_document(example_payload).task
    judge = EchoJudge()
    report = TaskGateRunner(registry_with(check_total, check_email), judge=judge).run(rubric_task, good_inputs())
    assert report.passed, report.review_reasons
    assert judge.calls > 0
