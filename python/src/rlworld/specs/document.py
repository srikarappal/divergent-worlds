"""The spec document: all five layers in one file, plus YAML IO and cross layer rules."""

import copy
from typing import Any, Literal

import yaml
from pydantic import ValidationError, model_validator

from rlworld.specs.models import ExperimentSpec, ImageSpec, RunSpec, SpecModel, TaskSpec, WorldSpec

VALUE_ERROR_PREFIX = "Value error, "


class SpecValidationError(ValueError):
    """A spec failed validation; problems holds one readable line per issue."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


class SpecDocument(SpecModel):
    spec_version: Literal[1] = 1
    experiment: ExperimentSpec
    image: ImageSpec
    world: WorldSpec
    task: TaskSpec
    run: RunSpec

    @model_validator(mode="after")
    def check_cross_layer_rules(self) -> "SpecDocument":
        problems = cross_layer_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def cross_layer_problems(document: SpecDocument) -> list[str]:
    problems = []
    if document.world.network.internet == "live" and document.task.split == "train":
        problems.append("world.network.internet: live internet is eval only, use replay_only or none for train tasks")
    if document.task.judge_rubric and document.run.judge is None:
        problems.append("run.judge: the task has a judge rubric but the run sets no judge endpoint")
    if document.run.type == "rl" and document.task.split != "train":
        problems.append("task.split: rl runs train only on train split tasks")
    payload = document.model_dump(mode="json")
    for sweep_path in sorted(document.experiment.sweep):
        if not path_exists(payload, sweep_path):
            problems.append(f"experiment.sweep: path {sweep_path} does not exist in this spec")
    return problems


def path_exists(payload: dict[str, Any], dotted_path: str) -> bool:
    current_value: Any = payload
    for key in dotted_path.split("."):
        if not isinstance(current_value, dict) or key not in current_value:
            return False
        current_value = current_value[key]
    return True


def set_path(payload: dict[str, Any], dotted_path: str, value: Any) -> dict[str, Any]:
    """Return a deep copy of payload with value placed at dotted_path."""
    updated_payload = copy.deepcopy(payload)
    *parent_keys, leaf_key = dotted_path.split(".")
    parent = updated_payload
    for key in parent_keys:
        child = parent.get(key)
        if not isinstance(child, dict):
            raise KeyError(f"{dotted_path}: {key} is not a mapping")
        parent = child
    parent[leaf_key] = copy.deepcopy(value)
    return updated_payload


def format_validation_errors(error: ValidationError) -> list[str]:
    problems = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "spec"
        message = detail["msg"].removeprefix(VALUE_ERROR_PREFIX)
        problems.append(f"{location}: {message}")
    return problems


def parse_spec_document(raw_document: dict[str, Any]) -> SpecDocument:
    try:
        return SpecDocument.model_validate(raw_document)
    except ValidationError as error:
        raise SpecValidationError(format_validation_errors(error)) from error


def load_spec_document(yaml_text: str) -> SpecDocument:
    raw_document = yaml.safe_load(yaml_text)
    if not isinstance(raw_document, dict):
        raise SpecValidationError(["spec: a spec document must be a YAML mapping"])
    return parse_spec_document(raw_document)


def dump_spec_document(document: SpecDocument) -> str:
    payload: dict[str, Any] = {"spec_version": document.spec_version}
    payload.update(document.model_dump(mode="json", exclude_defaults=True))
    return yaml.safe_dump(payload, sort_keys=False)
