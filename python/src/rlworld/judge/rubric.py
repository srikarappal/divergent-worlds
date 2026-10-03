"""Judge rubrics: markdown files whose YAML front matter lists binary items (DESIGN.md section 6.9).

The markdown body is guidance for the judge. Every rubric carries a content
digest, so editing the text without bumping the version still changes the
cache key and the provenance.
"""

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

FRONT_MATTER_DELIMITER = "---"
IDENTIFIER_PATTERN = r"^[a-z0-9_]+$"


class RubricError(ValueError):
    """A rubric file that cannot be loaded or is malformed."""


class RubricItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    question: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0.0)


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: str = Field(min_length=1)
    items: tuple[RubricItem, ...] = Field(min_length=1)
    guidance: str = ""
    content_digest: str

    @model_validator(mode="after")
    def check_unique_items(self) -> "Rubric":
        item_ids = [item.id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("rubric item ids must be unique")
        return self

    @property
    def pinned_version(self) -> str:
        return f"{self.version}+{self.content_digest.removeprefix('sha256:')[:12]}"


def parse_rubric(rubric_text: str) -> Rubric:
    lines = rubric_text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise RubricError("a rubric starts with YAML front matter between --- lines")
    closing_index = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == FRONT_MATTER_DELIMITER), None
    )
    if closing_index is None:
        raise RubricError("rubric front matter is not closed with ---")
    front_matter = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    if not isinstance(front_matter, dict):
        raise RubricError("rubric front matter must be a mapping")
    rubric_payload = {
        **front_matter,
        "guidance": "\n".join(lines[closing_index + 1 :]).strip(),
        "content_digest": f"sha256:{hashlib.sha256(rubric_text.encode('utf-8')).hexdigest()}",
    }
    try:
        return Rubric.model_validate(rubric_payload)
    except ValidationError as error:
        first_problem = error.errors()[0]
        location = ".".join(str(part) for part in first_problem["loc"]) or "rubric"
        raise RubricError(f"invalid rubric at {location}: {first_problem['msg']}") from error


class RubricLibrary:
    """Loads rubrics by the relative path a task names, never outside its root directory."""

    def __init__(self, root_directory: Path) -> None:
        self.root_directory = root_directory.resolve()
        self.loaded_rubrics: dict[str, Rubric] = {}

    def load(self, relative_path: str) -> Rubric:
        if relative_path in self.loaded_rubrics:
            return self.loaded_rubrics[relative_path]
        rubric_path = (self.root_directory / relative_path).resolve()
        if not rubric_path.is_relative_to(self.root_directory):
            raise RubricError(f"rubric path {relative_path} escapes the rubric root")
        if not rubric_path.is_file():
            raise RubricError(f"rubric {relative_path} does not exist")
        rubric = parse_rubric(rubric_path.read_text())
        self.loaded_rubrics[relative_path] = rubric
        return rubric
