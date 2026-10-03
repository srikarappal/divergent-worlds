import copy
import dataclasses
import json

import pytest

from rlworld.judge import InMemoryJudgeCache, JudgePromptBuilder, JudgeVerdictError, RubricJudge
from rlworld.rewards import CheckResult, EpisodeOutcome, FunctionVerifier, VerifierRegistry, evaluate_episode

ITEM_IDS = ("names_every_discrepancy", "states_the_reconciled_total", "professional_and_concise")
JUDGE_MODEL = "judge_model_2026_06"


def verdict_reply(passed=None, confidence=None) -> str:
    passed = passed or {}
    confidence = confidence or {}
    return json.dumps(
        {
            "items": [
                {
                    "id": item_id,
                    "passed": passed.get(item_id, True),
                    "evidence": f"{item_id} evidence",
                    "confidence": confidence.get(item_id, 0.95),
                }
                for item_id in ITEM_IDS
            ]
        }
    )


class ScriptedClient:
    """Returns queued replies in order and records every request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return self.replies.pop(0)


def make_judge(client, rubric_library, **options):
    return RubricJudge(client, rubric_library, JUDGE_MODEL, JudgePromptBuilder(b"secret"), **options)


def test_one_confident_sample_decides(judge_context, rubric_library):
    client = ScriptedClient([verdict_reply(passed={"professional_and_concise": False})])
    outcome = make_judge(client, rubric_library).judge(judge_context, ())
    assert [check.passed for check in outcome.checks] == [True, True, False]
    assert [check.weight for check in outcome.checks] == [1.0, 1.0, 0.5]
    assert outcome.checks[0].name == "judge summary_email_quality: names_every_discrepancy"
    assert outcome.checks[2].detail == "professional_and_concise evidence"
    assert len(client.requests) == 1
    assert (client.requests[0].sample_index, client.requests[0].temperature) == (0, 0.0)
    assert outcome.rubric_version.startswith("v3+")
    assert not outcome.cached


def test_identical_final_states_share_one_judge_call(judge_context, rubric_library):
    cache = InMemoryJudgeCache()
    client = ScriptedClient([verdict_reply(), verdict_reply()])
    judge = make_judge(client, rubric_library, cache=cache)
    first_outcome = judge.judge(judge_context, ())
    second_outcome = judge.judge(judge_context, ())
    assert second_outcome.cached and second_outcome.checks == first_outcome.checks
    assert len(client.requests) == 1
    assert (cache.hits, cache.misses) == (1, 1)
    changed_state = copy.deepcopy(judge_context.final_state)
    changed_state["mail"]["sent"][0]["body"] = "Different email"
    judge.judge(dataclasses.replace(judge_context, final_state=changed_state), ())
    assert len(client.requests) == 2


def test_unsure_items_take_a_majority_vote(judge_context, rubric_library):
    client = ScriptedClient(
        [
            verdict_reply(confidence={"professional_and_concise": 0.4}),
            verdict_reply(passed={"professional_and_concise": False, "names_every_discrepancy": False}),
            verdict_reply(passed={"professional_and_concise": False}),
        ]
    )
    outcome = make_judge(client, rubric_library).judge(judge_context, ())
    assert [check.passed for check in outcome.checks] == [True, True, False]
    assert outcome.checks[0].actual["samples"] == 1
    assert outcome.checks[2].actual == {
        "evidence": "professional_and_concise evidence",
        "samples": 3,
        "majority_votes": 2,
        "mean_confidence": 0.95,
    }
    assert [(request.sample_index, request.temperature) for request in client.requests] == [
        (0, 0.0),
        (1, 0.7),
        (2, 0.7),
    ]


def test_a_tied_vote_fails(judge_context, rubric_library):
    client = ScriptedClient(
        [verdict_reply(confidence={"states_the_reconciled_total": 0.5}),
         verdict_reply(passed={"states_the_reconciled_total": False})]
    )
    outcome = make_judge(client, rubric_library, ensemble_size=2).judge(judge_context, ())
    assert outcome.checks[1].passed is False


def test_malformed_replies_are_retried(judge_context, rubric_library):
    client = ScriptedClient(["no verdict here", verdict_reply()])
    outcome = make_judge(client, rubric_library).judge(judge_context, ())
    assert all(check.passed for check in outcome.checks)
    assert len(client.requests) == 2


def test_persistently_malformed_replies_raise(judge_context, rubric_library):
    client = ScriptedClient(["nope", "still nope"])
    with pytest.raises(JudgeVerdictError) as raised:
        make_judge(client, rubric_library).judge(judge_context, ())
    assert "after 2 attempts" in str(raised.value)


def passing_check(context):
    return [CheckResult(name="placeholder", tier="exact", passed=True)]


def passing_semantic_check(context):
    return [CheckResult(name="placeholder", tier="semantic", passed=True)]


def test_the_judge_plugs_into_the_reward_pipeline(judge_context, rubric_library):
    registry = VerifierRegistry()
    registry.register(FunctionVerifier("verifiers.excel.reconciliation_values_match", "1", "exact", passing_check))
    registry.register(
        FunctionVerifier("verifiers.mail.summary_lists_discrepant_invoices", "1", "semantic", passing_semantic_check)
    )
    client = ScriptedClient([verdict_reply()])
    judge = make_judge(client, rubric_library)
    final_state = judge_context.final_state
    initial_state = {"mail": final_state["mail"], "files": final_state["files"]}
    outcome = EpisodeOutcome(steps_taken=12, final_action="done")
    records = [
        evaluate_episode(
            judge_context.task,
            registry,
            initial_state,
            final_state,
            judge_context.ground_truth,
            outcome,
            judge=judge,
            instruction=judge_context.instruction,
            screenshots=judge_context.screenshots,
        )
        for _ in range(2)
    ]
    assert [record.termination for record in records] == ["success", "success"]
    assert [record.provenance.judge_cached for record in records] == [False, True]
    assert records[0].provenance.judge_model == JUDGE_MODEL
    assert len(client.requests) == 1
    assert client.requests[0].messages[1]["content"][-1]["uri"] == "s3://frames/episode_1/final.webp"
