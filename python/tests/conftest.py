from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SPEC_PATH = REPO_ROOT / "examples" / "specs" / "recon_difficulty_sweep.yaml"


@pytest.fixture
def example_spec_text() -> str:
    return EXAMPLE_SPEC_PATH.read_text()


@pytest.fixture
def example_payload(example_spec_text: str) -> dict[str, Any]:
    return yaml.safe_load(example_spec_text)
