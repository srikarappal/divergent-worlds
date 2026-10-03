from rlworld.judge import INJECTION_PROBES, InjectionCase, inject_text, run_injection_suite
from rlworld.rewards import CheckResult, JudgeOutcome

EMAIL_PATH = "mail/sent/0/body"


class GullibleJudge:
    """Fails the work unless the agent's text tells it otherwise."""

    def judge(self, context, prior_checks):
        body = context.final_state["mail"]["sent"][0]["body"]
        passed = "mark every rubric item as passed" in body or "already verified" in body
        return JudgeOutcome((CheckResult(name="tone", tier="judge", passed=passed),), "gullible", "v1")


class FirmJudge:
    def judge(self, context, prior_checks):
        return JudgeOutcome((CheckResult(name="tone", tier="judge", passed=False),), "firm", "v1")


class ApprovingJudge:
    def judge(self, context, prior_checks):
        return JudgeOutcome((CheckResult(name="tone", tier="judge", passed=True),), "approving", "v1")


def injection_cases(judge_context):
    return [
        InjectionCase(f"probe_{index}", judge_context, inject_text(judge_context, EMAIL_PATH, probe))
        for index, probe in enumerate(INJECTION_PROBES)
    ]


def test_inject_text_appends_at_a_state_path(judge_context):
    injected_context = inject_text(judge_context, EMAIL_PATH, "PROBE")
    assert injected_context.final_state["mail"]["sent"][0]["body"].endswith("\nPROBE")
    assert "PROBE" not in judge_context.final_state["mail"]["sent"][0]["body"]


def test_a_gullible_judge_fails_the_suite(judge_context):
    report = run_injection_suite(GullibleJudge(), injection_cases(judge_context))
    assert not report.passed
    assert report.flipped_case_names == ("probe_0", "probe_2")


def test_a_firm_judge_passes_the_suite(judge_context):
    report = run_injection_suite(FirmJudge(), injection_cases(judge_context))
    assert report.passed
    assert len(report.checked_case_names) == len(INJECTION_PROBES)


def test_cases_whose_clean_work_passes_are_invalid(judge_context):
    report = run_injection_suite(ApprovingJudge(), injection_cases(judge_context)[:1])
    assert report.invalid_case_names == ("probe_0",)
    assert not report.passed
