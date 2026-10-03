import copy

import pytest

from rlworld.rewards import (
    CheckResult,
    DuplicateVerifierError,
    EpisodeOutcome,
    FunctionVerifier,
    JudgeOutcome,
    RewardConfigurationError,
    RewardPolicy,
    RewardRecord,
    UnknownVerifierError,
    VerifierRegistry,
    evaluate_episode,
)
from rlworld.rewards.checks import cells_equal, entities_mentioned
from rlworld.specs import parse_spec_document

EXACT_VERIFIER = "verifiers.excel.reconciliation_values_match"
SEMANTIC_VERIFIER = "verifiers.mail.summary_lists_discrepant_invoices"
INITIAL_STATE = {
    "files": {
        "reconciliation.xlsx": {"sheets": {"Summary": {"cells": {"B2": {"value": None, "formula": None}}}}},
        "invoices.csv": {"rows": 12},
    },
    "mail": {"inbox": [{"id": "m1", "subject": "Q3 invoices"}], "sent": []},
    "browser": {"history": []},
}
GROUND_TRUTH = {"summary_cells": {"B2": 1250.0}, "discrepant_invoices": ["INV-1001", "INV-1007"]}


def final_state_with(total_value=1250.0, email_body="Discrepancies: INV-1001 and INV-1007"):
    state = copy.deepcopy(INITIAL_STATE)
    summary_cells = state["files"]["reconciliation.xlsx"]["sheets"]["Summary"]["cells"]
    summary_cells["B2"] = {"value": total_value, "formula": "=SUM(Invoices!C2:C13)"}
    state["mail"]["sent"] = [{"id": "s1", "to": "cfo@example.com", "body": email_body}]
    return state


def check_reconciliation_values(context):
    cells = context.final_state["files"]["reconciliation.xlsx"]["sheets"]["Summary"]["cells"]
    return [cells_equal("summary values", cells, context.ground_truth["summary_cells"])]


def check_summary_email(context):
    sent_messages = context.final_state["mail"]["sent"]
    body = sent_messages[-1]["body"] if sent_messages else ""
    return [entities_mentioned("summary names discrepancies", body, context.ground_truth["discrepant_invoices"])]


def crash(context):
    raise RuntimeError("cell extractor returned junk")


class RecordingJudge:
    def __init__(self, passed=True, error=None):
        self.passed = passed
        self.error = error
        self.calls = 0

    def judge(self, context, prior_checks):
        self.calls += 1
        if self.error:
            raise self.error
        verdict = CheckResult(name="email tone", tier="judge", passed=self.passed)
        return JudgeOutcome(checks=(verdict,), judge_model="judge_model_2026_06", rubric_version="v3")


@pytest.fixture
def task(example_payload):
    return parse_spec_document(example_payload).task


@pytest.fixture
def registry():
    verifier_registry = VerifierRegistry()
    verifier_registry.register(FunctionVerifier(EXACT_VERIFIER, "2", "exact", check_reconciliation_values))
    verifier_registry.register(FunctionVerifier(SEMANTIC_VERIFIER, "1", "semantic", check_summary_email))
    return verifier_registry


def score(task, registry, final_state, outcome=None, policy=None, judge=None, volatile_patterns=()):
    return evaluate_episode(
        task,
        registry,
        INITIAL_STATE,
        final_state,
        GROUND_TRUTH,
        outcome or EpisodeOutcome(steps_taken=30, final_action="done"),
        policy,
        judge or RecordingJudge(),
        volatile_patterns,
    )


def test_a_correct_episode_scores_one_with_provenance(task, registry):
    judge = RecordingJudge()
    record = score(task, registry, final_state_with(), judge=judge)
    assert (record.total, record.termination, record.counts_for_training) == (1.0, "success", True)
    assert judge.calls == 1
    assert [check.tier for check in record.checks] == ["exact", "semantic", "judge"]
    assert record.provenance.verifier_versions == {EXACT_VERIFIER: "2", SEMANTIC_VERIFIER: "1"}
    assert (record.provenance.judge_model, record.provenance.rubric_version) == ("judge_model_2026_06", "v3")
    assert record.provenance.state_hash.startswith("sha256:")
    assert not record.provenance.judge_skipped


def test_a_failed_check_skips_the_judge(task, registry):
    judge = RecordingJudge()
    record = score(task, registry, final_state_with(total_value=1200.0), judge=judge)
    assert (record.total, record.termination) == (0.0, "failure")
    assert judge.calls == 0
    assert record.provenance.judge_skipped
    assert record.checks[0].detail == "B2: expected 1250.0, got 1200.0"


def test_side_effects_gate_the_outcome(task, registry):
    final_state = final_state_with()
    del final_state["files"]["invoices.csv"]
    judge = RecordingJudge()
    record = score(task, registry, final_state, judge=judge)
    assert (record.total, record.termination) == (0.0, "failure")
    assert [violation.path for violation in record.side_effect_violations] == ["files/invoices.csv"]
    assert judge.calls == 0


def test_side_effects_can_be_a_penalty(task, registry):
    final_state = final_state_with()
    del final_state["files"]["invoices.csv"]
    record = score(task, registry, final_state, policy=RewardPolicy(side_effect_mode="penalty"))
    assert (record.total, record.termination) == (0.75, "success")
    assert record.components["side_effects"] == -0.25


def test_volatile_paths_are_not_side_effects(task, registry):
    final_state = final_state_with()
    final_state["browser"]["history"] = [{"id": "h1", "url": "https://crm.example.com"}]
    assert score(task, registry, final_state).termination == "failure"
    assert score(task, registry, final_state, volatile_patterns=["browser/history"]).termination == "success"


def test_env_errors_are_excluded_from_training(task, registry):
    record = score(task, registry, final_state_with(), outcome=EpisodeOutcome(steps_taken=3, env_error="restore failed"))
    assert (record.total, record.termination, record.counts_for_training) == (0.0, "env_error", False)
    assert record.error_detail == "restore failed"
    assert record.components == {}


def test_agent_errors_count_against_the_policy(task, registry):
    outcome = EpisodeOutcome(steps_taken=3, agent_error="unparsable action")
    record = score(task, registry, final_state_with(), outcome=outcome)
    assert (record.termination, record.counts_for_training) == ("agent_error", True)


def test_a_crashing_verifier_is_an_env_error(task):
    crashing_registry = VerifierRegistry()
    crashing_registry.register(FunctionVerifier(EXACT_VERIFIER, "1", "exact", crash))
    crashing_registry.register(FunctionVerifier(SEMANTIC_VERIFIER, "1", "semantic", check_summary_email))
    record = score(task, crashing_registry, final_state_with())
    assert (record.termination, record.counts_for_training) == ("env_error", False)
    assert "raised RuntimeError" in record.error_detail


def test_a_failing_judge_call_is_an_env_error(task, registry):
    record = score(task, registry, final_state_with(), judge=RecordingJudge(error=TimeoutError("judge timed out")))
    assert record.termination == "env_error"
    assert "TimeoutError" in record.error_detail


def test_a_negative_verdict_fails_the_episode(task, registry):
    record = score(task, registry, final_state_with(), judge=RecordingJudge(passed=False))
    assert (record.total, record.termination) == (0.0, "failure")


def test_running_out_of_steps(task, registry):
    unfinished = EpisodeOutcome(steps_taken=50)
    assert score(task, registry, final_state_with(total_value=1.0), outcome=unfinished).termination == "max_steps"
    assert score(task, registry, final_state_with(), outcome=unfinished).termination == "success"
    strict_record = score(task, registry, final_state_with(), outcome=unfinished,
                          policy=RewardPolicy(require_finish=True))
    assert (strict_record.total, strict_record.termination) == (0.0, "max_steps")


def test_partial_credit_weighs_every_check(task, registry):
    record = score(task, registry, final_state_with(total_value=1.0), policy=RewardPolicy(mode="partial_credit"))
    assert record.total == pytest.approx(2 / 3)
    assert record.termination == "failure"


def test_efficiency_penalizes_extra_steps(task, registry):
    outcome = EpisodeOutcome(steps_taken=40, final_action="done")
    record = score(task, registry, final_state_with(), outcome=outcome, policy=RewardPolicy(efficiency_weight=0.5))
    assert record.components["efficiency"] == pytest.approx(-0.1)
    assert record.total == pytest.approx(0.9)


def test_a_rubric_needs_a_judge(task, registry):
    with pytest.raises(RewardConfigurationError):
        evaluate_episode(task, registry, INITIAL_STATE, final_state_with(), GROUND_TRUTH,
                         EpisodeOutcome(steps_taken=3, final_action="done"))
    no_rubric_task = task.model_copy(update={"judge_rubric": None})
    record = evaluate_episode(no_rubric_task, registry, INITIAL_STATE, final_state_with(), GROUND_TRUTH,
                              EpisodeOutcome(steps_taken=3, final_action="done"))
    assert record.termination == "success"


def test_registry_rules(task):
    empty_registry = VerifierRegistry()
    with pytest.raises(UnknownVerifierError):
        score(task, empty_registry, final_state_with())
    mismatched_registry = VerifierRegistry()
    mismatched_registry.register(FunctionVerifier(EXACT_VERIFIER, "1", "semantic", check_reconciliation_values))
    with pytest.raises(ValueError):
        score(task, mismatched_registry, final_state_with())
    verifier = FunctionVerifier(SEMANTIC_VERIFIER, "1", "semantic", check_summary_email)
    mismatched_registry.register(verifier)
    mismatched_registry.register(verifier)
    with pytest.raises(DuplicateVerifierError):
        mismatched_registry.register(FunctionVerifier(SEMANTIC_VERIFIER, "2", "semantic", check_summary_email))


def test_records_round_trip_through_json(task, registry):
    final_state = final_state_with()
    del final_state["files"]["invoices.csv"]
    record = score(task, registry, final_state)
    assert RewardRecord.model_validate_json(record.model_dump_json()) == record
