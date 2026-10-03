"""Score one episode: diff, side effects, exact then semantic verifiers, then the judge, then compose.

The caller (the reward service) freezes input and extracts state before this
runs and tears the VM down only after it returns (DESIGN.md section 6.9).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from rlworld.rewards.diff import diff_states, side_effect_violations
from rlworld.rewards.records import (
    CheckResult,
    EpisodeOutcome,
    Provenance,
    RewardPolicy,
    RewardRecord,
    StateChange,
    TerminationLabel,
)
from rlworld.rewards.verifiers import VerifierContext, VerifierRegistry
from rlworld.specs.digest import content_digest
from rlworld.specs.models import TaskSpec


@dataclass(frozen=True)
class JudgeOutcome:
    checks: tuple[CheckResult, ...]
    judge_model: str
    rubric_version: str
    cached: bool = False


class JudgeTier(Protocol):
    def judge(self, context: VerifierContext, prior_checks: Sequence[CheckResult]) -> JudgeOutcome: ...


class RewardConfigurationError(ValueError):
    """The task asks for something the pipeline was not given, such as a judge."""


def evaluate_episode(
    task: TaskSpec,
    registry: VerifierRegistry,
    initial_state: dict[str, Any],
    final_state: dict[str, Any],
    ground_truth: dict[str, Any],
    outcome: EpisodeOutcome,
    policy: RewardPolicy | None = None,
    judge: JudgeTier | None = None,
    volatile_patterns: Sequence[str] = (),
    instruction: str = "",
    screenshots: Sequence[str] = (),
) -> RewardRecord:
    reward_policy = policy or RewardPolicy()
    exact_verifiers = registry.resolve(task.verifiers.exact, "exact")
    semantic_verifiers = registry.resolve(task.verifiers.semantic, "semantic")
    if task.judge_rubric and judge is None:
        raise RewardConfigurationError("the task has a judge rubric but no judge was provided")
    base_provenance = {
        "verifier_versions": {
            verifier.name: verifier.version for verifier in (*exact_verifiers, *semantic_verifiers)
        },
        "state_hash": content_digest(final_state),
        "ground_truth_digest": content_digest(ground_truth),
        "reward_policy": reward_policy,
    }
    if outcome.env_error:
        return error_record("env_error", outcome.env_error, base_provenance)
    if outcome.agent_error:
        return error_record("agent_error", outcome.agent_error, base_provenance)

    changes = tuple(diff_states(initial_state, final_state))
    violations = tuple(side_effect_violations(changes, task.side_effects.allowed_changes, volatile_patterns))
    context = VerifierContext(
        task=task,
        initial_state=initial_state,
        final_state=final_state,
        changes=changes,
        ground_truth=ground_truth,
        final_action=outcome.final_action,
        answer_text=outcome.answer_text,
        instruction=instruction,
        screenshots=tuple(screenshots),
    )
    checks: list[CheckResult] = []
    for verifier in (*exact_verifiers, *semantic_verifiers):
        try:
            checks.extend(verifier.verify(context))
        except Exception as error:  # task authored code: a crash is an env error, never a zero reward
            detail = f"verifier {verifier.name} raised {type(error).__name__}: {error}"
            return error_record("env_error", detail, base_provenance)

    judge_provenance: dict[str, Any] = {}
    if task.judge_rubric:
        if reward_policy.mode == "binary" and outcome_already_lost(checks, violations, reward_policy):
            judge_provenance["judge_skipped"] = True
        else:
            try:
                judge_outcome = judge.judge(context, tuple(checks))
            except Exception as error:  # endpoint failures and timeouts are env errors
                return error_record("env_error", f"judge failed: {type(error).__name__}: {error}", base_provenance)
            checks.extend(judge_outcome.checks)
            judge_provenance = {
                "judge_model": judge_outcome.judge_model,
                "rubric_version": judge_outcome.rubric_version,
                "judge_cached": judge_outcome.cached,
            }
    provenance = Provenance(**base_provenance, **judge_provenance)
    return compose_reward(task, outcome, tuple(checks), violations, reward_policy, provenance)


def outcome_already_lost(checks: Sequence[CheckResult], violations: Sequence[StateChange], policy: RewardPolicy) -> bool:
    """In binary mode a failed check or a gated side effect decides the outcome, so the judge is skipped."""
    return any(not check.passed for check in checks) or (bool(violations) and policy.side_effect_mode == "gate")


def outcome_score(checks: Sequence[CheckResult], policy: RewardPolicy) -> float:
    if not checks:
        return 0.0
    all_passed = all(check.passed for check in checks)
    if policy.mode == "binary":
        return 1.0 if all_passed else 0.0
    total_weight = sum(check.weight for check in checks)
    if total_weight == 0:
        return 1.0 if all_passed else 0.0
    return sum(check.weight for check in checks if check.passed) / total_weight


def compose_reward(
    task: TaskSpec,
    outcome: EpisodeOutcome,
    checks: tuple[CheckResult, ...],
    violations: tuple[StateChange, ...],
    policy: RewardPolicy,
    provenance: Provenance,
) -> RewardRecord:
    unfinished = policy.require_finish and outcome.final_action is None
    gated_violation = bool(violations) and policy.side_effect_mode == "gate"
    outcome_value = 0.0 if unfinished or gated_violation else outcome_score(checks, policy)
    side_effect_value = 0.0
    if violations and policy.side_effect_mode == "penalty":
        side_effect_value = -min(policy.side_effect_penalty * len(violations), policy.max_side_effect_penalty)
    efficiency_value = 0.0
    if policy.efficiency_weight and outcome_value > 0:
        extra_steps = max(0, outcome.steps_taken - task.difficulty.oracle_steps)
        efficiency_value = -policy.efficiency_weight * min(1.0, extra_steps / task.max_steps)
    succeeded = bool(checks) and all(check.passed for check in checks) and not gated_violation and not unfinished
    termination: TerminationLabel = "success"
    if not succeeded:
        ran_out_of_steps = outcome.final_action is None and outcome.steps_taken >= task.max_steps
        termination = "max_steps" if ran_out_of_steps else "failure"
    return RewardRecord(
        total=max(-1.0, min(1.0, outcome_value + side_effect_value + efficiency_value)),
        components={"outcome": outcome_value, "side_effects": side_effect_value, "efficiency": efficiency_value},
        termination=termination,
        counts_for_training=True,
        checks=checks,
        side_effect_violations=violations,
        error_detail=None if checks else "no checks ran",
        provenance=provenance,
    )


def error_record(termination: TerminationLabel, detail: str, base_provenance: dict[str, Any]) -> RewardRecord:
    """Env errors are excluded from training; agent errors are the policy's fault and count."""
    return RewardRecord(
        total=0.0,
        components={},
        termination=termination,
        counts_for_training=termination != "env_error",
        error_detail=detail,
        provenance=Provenance(**base_provenance),
    )


def env_error_record(
    detail: str, final_state: dict[str, Any], ground_truth: dict[str, Any], policy: RewardPolicy | None = None
) -> RewardRecord:
    """For failures outside the pipeline, such as an extraction that never returned."""
    base_provenance = {
        "state_hash": content_digest(final_state),
        "ground_truth_digest": content_digest(ground_truth),
        "reward_policy": policy or RewardPolicy(),
    }
    return error_record("env_error", detail, base_provenance)
