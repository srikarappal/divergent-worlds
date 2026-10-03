from pathlib import Path

from rlworld.specs.schema import render_spec_document_schema, spec_document_json_schema, write_spec_document_schema

COMMITTED_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "spec_document.schema.json"


def test_schema_covers_every_layer():
    schema = spec_document_json_schema()
    assert {"spec_version", "experiment", "image", "world", "task", "run"} == set(schema["properties"])


def test_committed_schema_matches_the_models():
    assert COMMITTED_SCHEMA_PATH.read_text() == render_spec_document_schema(), (
        "schemas/spec_document.schema.json is stale; regenerate it with rlworld-export-schema"
    )


def test_write_schema_creates_parent_directories(tmp_path):
    output_path = tmp_path / "nested" / "spec.schema.json"
    write_spec_document_schema(output_path)
    assert output_path.read_text() == render_spec_document_schema()
