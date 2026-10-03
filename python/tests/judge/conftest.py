from pathlib import Path

import pytest

from rlworld.judge import RubricLibrary
from rlworld.rewards import VerifierContext
from rlworld.specs import parse_spec_document

EXAMPLES_DIRECTORY = Path(__file__).resolve().parents[3] / "examples"
FINAL_STATE = {
    "mail": {"sent": [{"id": "s1", "body": "Discrepancies: INV-1001 and INV-1007. Reconciled total 1250."}]},
    "files": {"reconciliation.xlsx": {"cells": {"B2": {"value": 1250}}}},
}


@pytest.fixture
def rubric_library() -> RubricLibrary:
    return RubricLibrary(EXAMPLES_DIRECTORY)


@pytest.fixture
def judge_task(example_payload):
    return parse_spec_document(example_payload).task


@pytest.fixture
def judge_context(judge_task) -> VerifierContext:
    return VerifierContext(
        task=judge_task,
        initial_state={},
        final_state=FINAL_STATE,
        changes=(),
        ground_truth={"discrepant_invoices": ["INV-1001", "INV-1007"], "reconciled_total": 1250},
        final_action="done",
        instruction="Reconcile the Q3 invoices and email the CFO a short summary.",
        screenshots=("s3://frames/episode_1/final.webp",),
    )
