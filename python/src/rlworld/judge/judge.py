"""Rubric judge: the third reward tier, used only where code cannot decide (DESIGN.md section 6.9).

One sample at temperature zero decides most episodes. Items the judge is unsure
about get extra samples and a majority vote, where a tie fails.
"""

from collections.abc import Sequence
from typing import Protocol

from rlworld.judge.cache import InMemoryJudgeCache, JudgeCache, JudgeCacheKey
from rlworld.judge.prompt import JudgePromptBuilder, JudgeRequest
from rlworld.judge.rubric import Rubric, RubricLibrary
from rlworld.judge.verdict import JudgeVerdictError, Verdict, parse_verdict
from rlworld.rewards.pipeline import JudgeOutcome
from rlworld.rewards.records import CheckResult
from rlworld.rewards.verifiers import VerifierContext
from rlworld.specs.digest import content_digest


class JudgeClient(Protocol):
    """Sends one judge request to an endpoint and returns the raw reply text."""

    def complete(self, request: JudgeRequest) -> str: ...


class RubricJudge:
    def __init__(
        self,
        client: JudgeClient,
        rubric_library: RubricLibrary,
        judge_model: str,
        prompt_builder: JudgePromptBuilder,
        cache: JudgeCache | None = None,
        ensemble_size: int = 3,
        confidence_threshold: float = 0.7,
        ensemble_temperature: float = 0.7,
        max_attempts: int = 2,
    ) -> None:
        if ensemble_size < 1 or max_attempts < 1:
            raise ValueError("ensemble_size and max_attempts must be at least 1")
        self.client = client
        self.rubric_library = rubric_library
        self.judge_model = judge_model
        self.prompt_builder = prompt_builder
        self.cache = cache if cache is not None else InMemoryJudgeCache()
        self.ensemble_size = ensemble_size
        self.confidence_threshold = confidence_threshold
        self.ensemble_temperature = ensemble_temperature
        self.max_attempts = max_attempts

    def judge(self, context: VerifierContext, prior_checks: Sequence[CheckResult]) -> JudgeOutcome:
        if not context.task.judge_rubric:
            raise ValueError("this task has no judge rubric")
        rubric = self.rubric_library.load(context.task.judge_rubric)
        cache_key = JudgeCacheKey(
            state_hash=content_digest(context.final_state),
            rubric_id=rubric.id,
            rubric_version=rubric.pinned_version,
            judge_model=self.judge_model,
            instruction_digest=content_digest(context.instruction),
            reference_digest=content_digest(context.ground_truth),
        )
        cached_checks = self.cache.get(cache_key)
        if cached_checks is not None:
            return JudgeOutcome(cached_checks, self.judge_model, rubric.pinned_version, cached=True)
        first_verdict = self.request_verdict(context, rubric, sample_index=0, temperature=0.0)
        unsure_item_ids = frozenset(
            item.id for item in first_verdict.items if item.confidence < self.confidence_threshold
        )
        verdicts = [first_verdict]
        if unsure_item_ids and self.ensemble_size > 1:
            for sample_index in range(1, self.ensemble_size):
                verdicts.append(self.request_verdict(context, rubric, sample_index, self.ensemble_temperature))
        checks = tuple(combine_verdicts(rubric, verdicts, unsure_item_ids))
        self.cache.put(cache_key, checks)
        return JudgeOutcome(checks, self.judge_model, rubric.pinned_version)

    def request_verdict(self, context: VerifierContext, rubric: Rubric, sample_index: int, temperature: float) -> Verdict:
        request = self.prompt_builder.build(context, rubric, self.judge_model, sample_index, temperature)
        last_error: JudgeVerdictError | None = None
        for _ in range(self.max_attempts):
            try:
                return parse_verdict(self.client.complete(request), rubric)
            except JudgeVerdictError as error:
                last_error = error
        raise JudgeVerdictError(f"no valid verdict after {self.max_attempts} attempts: {last_error}")


def combine_verdicts(rubric: Rubric, verdicts: Sequence[Verdict], voted_item_ids: frozenset[str]) -> list[CheckResult]:
    """Confident items keep the first sample; unsure items take a majority vote where a tie fails."""
    verdict_maps = [verdict.by_item() for verdict in verdicts]
    checks = []
    for rubric_item in rubric.items:
        if rubric_item.id in voted_item_ids:
            item_verdicts = [verdict_map[rubric_item.id] for verdict_map in verdict_maps]
        else:
            item_verdicts = [verdict_maps[0][rubric_item.id]]
        pass_votes = sum(1 for item_verdict in item_verdicts if item_verdict.passed)
        passed = pass_votes * 2 > len(item_verdicts)
        majority = [item_verdict for item_verdict in item_verdicts if item_verdict.passed == passed]
        evidence = majority[0].evidence
        checks.append(
            CheckResult(
                name=f"judge {rubric.id}: {rubric_item.id}",
                tier="judge",
                passed=passed,
                weight=rubric_item.weight,
                detail="" if passed else evidence,
                expected=rubric_item.question,
                actual={
                    "evidence": evidence,
                    "samples": len(item_verdicts),
                    "majority_votes": len(majority),
                    "mean_confidence": sum(item_verdict.confidence for item_verdict in majority) / len(majority),
                },
            )
        )
    return checks
