"""Parse and check structured judge verdicts: one strict boolean per rubric item."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from rlworld.judge.rubric import Rubric

CODE_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class JudgeVerdictError(ValueError):
    """A judge reply that is not a complete, well formed verdict."""


class ItemVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    passed: StrictBool
    evidence: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ItemVerdict, ...]

    def by_item(self) -> dict[str, ItemVerdict]:
        return {item.id: item for item in self.items}


def extract_json_object(response_text: str) -> str:
    fenced_match = CODE_FENCE_PATTERN.search(response_text)
    if fenced_match:
        return fenced_match.group(1)
    start_index = response_text.find("{")
    end_index = response_text.rfind("}")
    if start_index == -1 or end_index <= start_index:
        raise JudgeVerdictError("the judge reply holds no JSON object")
    return response_text[start_index : end_index + 1]


def parse_verdict(response_text: str, rubric: Rubric) -> Verdict:
    try:
        verdict = Verdict.model_validate(json.loads(extract_json_object(response_text)))
    except (json.JSONDecodeError, ValidationError) as error:
        raise JudgeVerdictError(f"malformed judge verdict: {error}") from error
    expected_ids = [item.id for item in rubric.items]
    returned_ids = [item.id for item in verdict.items]
    problems = []
    duplicate_ids = sorted({item_id for item_id in returned_ids if returned_ids.count(item_id) > 1})
    if duplicate_ids:
        problems.append(f"duplicate items {duplicate_ids}")
    unknown_ids = sorted(set(returned_ids) - set(expected_ids))
    if unknown_ids:
        problems.append(f"unknown items {unknown_ids}")
    missing_ids = [item_id for item_id in expected_ids if item_id not in returned_ids]
    if missing_ids:
        problems.append(f"missing items {missing_ids}")
    if problems:
        raise JudgeVerdictError("; ".join(problems))
    return verdict
