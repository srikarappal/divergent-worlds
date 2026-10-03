"""Task validation gates (DESIGN.md section 6.10).

A task ships only when its verifiers fail the untouched start state, pass the
scripted oracle, give identical results across oracle reruns, accept every
alternative valid solution, reject every near miss and a strong agent solves
it at least once in k tries. A failing gate sends the task to human review.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rlworld.rewards.pipeline import JudgeTier, evaluate_episode
from rlworld.rewards.records import EpisodeOutcome, RewardPolicy, RewardRecord
from rlworld.rewards.verifiers import VerifierRegistry
from rlworld.specs.digest import spec_digest
from rlworld.specs.models import TaskSpec


@dataclass(frozen=True)
class SolutionAttempt:
    """A final state from running a script or an agent in a fresh env forked from the task start state."""

    name: str
    final_state: dict[str, Any]
    outcome: EpisodeOutcome


@dataclass(frozen=True)
class GateInputs:
    initial_state: dict[str, Any]
    ground_truth: dict[str, Any]
    oracle_runs: tuple[SolutionAttempt, ...]
    alternative_solutions: tuple[SolutionAttempt, ...] = ()
    near_misses: tuple[SolutionAttempt, ...] = ()
    agent_attempts: tuple[SolutionAttempt, ...] = ()
    instruction: str = ""


@dataclass(frozen=True)
class GateConfig:
    rerun_count: int = 3
    solvability_tries: int = 8


@dataclass(frozen=True)
class AttemptSummary:
    name: str
    termination: str
    total: float
    failed_checks: tuple[str, ...]
    side_effect_paths: tuple[str, ...]
    error_detail: str | None


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str
    attempts: tuple[AttemptSummary, ...] = ()


@dataclass(frozen=True)
class GateReport:
    task_digest: str
    results: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def review_reasons(self) -> list[str]:
        return [f"{result.name}: {result.detail}" for result in self.results if not result.passed]

    def result(self, gate_name: str) -> GateResult:
        return next(result for result in self.results if result.name == gate_name)


def summarize_attempt(attempt_name: str, record: RewardRecord) -> AttemptSummary:
    return AttemptSummary(
        name=attempt_name,
        termination=record.termination,
        total=record.total,
        failed_checks=tuple(f"{check.name}: {check.detail}" for check in record.checks if not check.passed),
        side_effect_paths=tuple(violation.path for violation in record.side_effect_violations),
        error_detail=record.error_detail,
    )


def describe_failure(summary: AttemptSummary) -> str:
    if summary.error_detail:
        return f"{summary.name} ({summary.termination}: {summary.error_detail})"
    reasons = list(summary.failed_checks) + [f"side effect at {path}" for path in summary.side_effect_paths]
    return f"{summary.name} ({'; '.join(reasons) or summary.termination})"


class TaskGateRunner:
    def __init__(
        self,
        registry: VerifierRegistry,
        judge: JudgeTier | None = None,
        policy: RewardPolicy | None = None,
        config: GateConfig | None = None,
        volatile_patterns: Sequence[str] = (),
    ) -> None:
        self.registry = registry
        self.judge = judge
        self.policy = policy
        self.config = config or GateConfig()
        self.volatile_patterns = volatile_patterns

    def run(self, task: TaskSpec, inputs: GateInputs) -> GateReport:
        results = (
            self.null_action_gate(task, inputs),
            self.oracle_gate(task, inputs),
            self.rerun_stability_gate(task, inputs),
            self.alternative_solution_gate(task, inputs),
            self.near_miss_gate(task, inputs),
            self.solvability_gate(task, inputs),
        )
        return GateReport(task_digest=spec_digest(task), results=results)

    def evaluate(self, task: TaskSpec, inputs: GateInputs, attempt: SolutionAttempt) -> tuple[RewardRecord, AttemptSummary]:
        record = evaluate_episode(
            task,
            self.registry,
            inputs.initial_state,
            attempt.final_state,
            inputs.ground_truth,
            attempt.outcome,
            self.policy,
            self.judge,
            self.volatile_patterns,
            instruction=inputs.instruction,
        )
        return record, summarize_attempt(attempt.name, record)

    def evaluate_all(self, task: TaskSpec, inputs: GateInputs, attempts: Sequence[SolutionAttempt]) -> list[AttemptSummary]:
        return [self.evaluate(task, inputs, attempt)[1] for attempt in attempts]

    def null_action_gate(self, task: TaskSpec, inputs: GateInputs) -> GateResult:
        """The agent claims done without touching anything; that must never succeed."""
        untouched_attempt = SolutionAttempt(
            "null_action", inputs.initial_state, EpisodeOutcome(steps_taken=0, final_action="done")
        )
        record, summary = self.evaluate(task, inputs, untouched_attempt)
        if record.termination == "env_error":
            return GateResult("null_action", False, f"evaluation errored: {record.error_detail}", (summary,))
        passed = record.termination != "success"
        detail = "" if passed else "the verifiers pass on the untouched start state"
        return GateResult("null_action", passed, detail, (summary,))

    def oracle_gate(self, task: TaskSpec, inputs: GateInputs) -> GateResult:
        if not inputs.oracle_runs:
            return GateResult("oracle", False, "no oracle run was provided")
        record, summary = self.evaluate(task, inputs, inputs.oracle_runs[0])
        if record.side_effect_violations:
            detail = (
                f"the oracle changes paths outside side_effects.allowed_changes: {list(summary.side_effect_paths)}"
            )
            return GateResult("oracle", False, detail, (summary,))
        if record.termination != "success":
            return GateResult("oracle", False, f"the oracle does not pass: {describe_failure(summary)}", (summary,))
        return GateResult("oracle", True, "", (summary,))

    def rerun_stability_gate(self, task: TaskSpec, inputs: GateInputs) -> GateResult:
        oracle_runs = inputs.oracle_runs[: self.config.rerun_count]
        if len(oracle_runs) < self.config.rerun_count:
            detail = f"needs {self.config.rerun_count} oracle runs, got {len(oracle_runs)}"
            return GateResult("rerun_stability", False, detail)
        summaries = self.evaluate_all(task, inputs, oracle_runs)
        outcomes = {(summary.termination, summary.total, summary.failed_checks) for summary in summaries}
        passed = len(outcomes) == 1
        detail = "" if passed else "oracle reruns disagree: " + ", ".join(
            f"{summary.name}={summary.termination}" for summary in summaries
        )
        return GateResult("rerun_stability", passed, detail, tuple(summaries))

    def alternative_solution_gate(self, task: TaskSpec, inputs: GateInputs) -> GateResult:
        if not inputs.alternative_solutions:
            return GateResult(
                "alternative_solution", False, "no alternative valid solution was provided, so false negatives go unseen"
            )
        summaries = self.evaluate_all(task, inputs, inputs.alternative_solutions)
        rejected = [summary for summary in summaries if summary.termination != "success"]
        detail = "" if not rejected else "the verifiers reject valid solutions: " + ", ".join(
            describe_failure(summary) for summary in rejected
        )
        return GateResult("alternative_solution", not rejected, detail, tuple(summaries))

    def near_miss_gate(self, task: TaskSpec, inputs: GateInputs) -> GateResult:
        if not inputs.near_misses:
            return GateResult("near_miss", False, "no near miss was provided, so false positives go unseen")
        summaries = self.evaluate_all(task, inputs, inputs.near_misses)
        accepted = [summary.name for summary in summaries if summary.termination == "success"]
        errored = [describe_failure(summary) for summary in summaries if summary.termination == "env_error"]
        problems = []
        if accepted:
            problems.append(f"the verifiers accept near misses: {accepted}")
        if errored:
            problems.append(f"evaluation errored: {errored}")
        return GateResult("near_miss", not problems, "; ".join(problems), tuple(summaries))

    def solvability_gate(self, task: TaskSpec, inputs: GateInputs) -> GateResult:
        agent_attempts = inputs.agent_attempts[: self.config.solvability_tries]
        if not agent_attempts:
            return GateResult("solvability", False, "no strong agent attempts were provided")
        summaries = self.evaluate_all(task, inputs, agent_attempts)
        solved_count = sum(1 for summary in summaries if summary.termination == "success")
        detail = f"a strong agent solved {solved_count} of {len(summaries)} tries"
        return GateResult("solvability", solved_count > 0, detail, tuple(summaries))
