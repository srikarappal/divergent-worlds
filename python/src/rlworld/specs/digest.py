"""Canonical content digests for specs (DESIGN.md sections 3 and 7)."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from rlworld.specs.document import SpecDocument

RUN_LAYERS = ("image", "world", "task", "run")


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_digest(payload: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


def spec_digest(spec: BaseModel) -> str:
    """Digest over non default values, so adding a defaulted field keeps old digests stable."""
    return content_digest(spec.model_dump(mode="json", exclude_defaults=True))


def layer_digests(document: SpecDocument) -> dict[str, str]:
    return {layer_name: spec_digest(getattr(document, layer_name)) for layer_name in RUN_LAYERS}


def run_spec_digest(document: SpecDocument) -> str:
    """Identity of what actually runs; experiment notes and budgets do not change it."""
    return content_digest({"spec_version": document.spec_version, "layers": layer_digests(document)})


def document_digest(document: SpecDocument) -> str:
    """Identity of the saved document, experiment layer included."""
    return content_digest(
        {
            "spec_version": document.spec_version,
            "experiment": spec_digest(document.experiment),
            "layers": layer_digests(document),
        }
    )
