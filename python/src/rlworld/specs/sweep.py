"""Expand an experiment's sweep into concrete, validated run documents."""

import math
import random
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from rlworld.specs.digest import run_spec_digest
from rlworld.specs.document import SpecDocument, SpecValidationError, format_validation_errors, set_path


@dataclass(frozen=True)
class SweepPoint:
    index: int
    run_name: str
    assignments: dict[str, Any]
    document: SpecDocument
    run_digest: str


def expand_sweep(document: SpecDocument) -> list[SweepPoint]:
    """One point per run; points are validated so a bad sweep value fails before launch."""
    experiment = document.experiment
    sweep_paths = sorted(experiment.sweep)
    value_lists = [experiment.sweep[sweep_path] for sweep_path in sweep_paths]
    combination_count = math.prod(len(values) for values in value_lists)
    base_payload = set_path(document.model_dump(mode="json"), "experiment.sweep", {})
    base_payload = set_path(base_payload, "experiment.sweep_mode", "grid")
    base_payload = set_path(base_payload, "experiment.random_samples", None)
    sweep_points = []
    for point_index, combination_index in enumerate(select_combinations(document, combination_count)):
        assignments = dict(zip(sweep_paths, decode_combination(combination_index, value_lists)))
        point_document = build_point_document(base_payload, point_index, assignments)
        sweep_points.append(
            SweepPoint(
                index=point_index,
                run_name=f"{experiment.name}__{point_index:03d}",
                assignments=assignments,
                document=point_document,
                run_digest=run_spec_digest(point_document),
            )
        )
    return sweep_points


def select_combinations(document: SpecDocument, combination_count: int) -> list[int]:
    experiment = document.experiment
    if experiment.sweep_mode == "grid":
        return list(range(combination_count))
    sample_count = min(experiment.random_samples or combination_count, combination_count)
    return sorted(random.Random(experiment.random_seed).sample(range(combination_count), sample_count))


def decode_combination(combination_index: int, value_lists: list[list[Any]]) -> list[Any]:
    """Mixed radix decode; the last sweep path varies fastest."""
    chosen_values = []
    remainder = combination_index
    for values in reversed(value_lists):
        remainder, position = divmod(remainder, len(values))
        chosen_values.append(values[position])
    return list(reversed(chosen_values))


def build_point_document(base_payload: dict[str, Any], point_index: int, assignments: dict[str, Any]) -> SpecDocument:
    point_payload = base_payload
    for sweep_path, sweep_value in assignments.items():
        point_payload = set_path(point_payload, sweep_path, sweep_value)
    try:
        return SpecDocument.model_validate(point_payload)
    except ValidationError as error:
        assignment_text = ", ".join(f"{sweep_path}={sweep_value}" for sweep_path, sweep_value in assignments.items())
        problems = [
            f"sweep point {point_index} ({assignment_text}): {problem}" for problem in format_validation_errors(error)
        ]
        raise SpecValidationError(problems) from error
