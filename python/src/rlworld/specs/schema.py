"""Export the spec JSON Schema shared by the Go services and the web console."""

import argparse
import json
from pathlib import Path
from typing import Any

from rlworld.specs.document import SpecDocument


def spec_document_json_schema() -> dict[str, Any]:
    return SpecDocument.model_json_schema()


def render_spec_document_schema() -> str:
    return json.dumps(spec_document_json_schema(), indent=2, sort_keys=True) + "\n"


def write_spec_document_schema(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_spec_document_schema())


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the spec document JSON Schema")
    parser.add_argument("output_path", type=Path)
    arguments = parser.parse_args()
    write_spec_document_schema(arguments.output_path)


if __name__ == "__main__":
    main()
