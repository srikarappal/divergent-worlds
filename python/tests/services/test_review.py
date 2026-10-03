import json

import pytest
from support.experiment_fakes import build_experiment_service, eval_payload
from support.trajectory_fakes import episode

from rlworld.actions.models import Observation
from rlworld.analysis.failures import LlmFailureLabeler
from rlworld.rewards.records import CheckResult, Provenance, RewardPolicy, RewardRecord
from rlworld.services.experimentd_http import create_app
from rlworld.services.experiments import UnknownRecordError
from rlworld.services.review import EpisodeFilter, ReviewError, ReviewRequest, ReviewService
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.trajectories import EpisodeKey, InMemoryObjectStore, ModelTurn, StepRecord, TrajectoryStore

INITIAL_STATE = {"files": {"reconciliation.xlsx": {"cells": {"B2": {"value": None}}}}}
FINAL_STATE = {"files": {"reconciliation.xlsx": {"cells": {"B2": {"value": 1200.0}}}}}


class VerdictClient:
    """Stands in for the judge endpoint the LLM failure labeler calls."""

    def complete(self, request):
        return json.dumps({"category": "verifier_false_negative", "summary": "the total is right but formatted",
                           "confidence": 0.8})


def scored(total, termination, failed_checks=(), counts_for_training=True):
    checks = tuple(CheckResult(name=name, tier="exact", passed=False, detail=f"{name} differs")
                   for name in failed_checks)
    provenance = Provenance(state_hash="sha256:s", ground_truth_digest="sha256:g", reward_policy=RewardPolicy())
    return RewardRecord(total=total, components={"outcome": total}, termination=termination,
                        counts_for_training=counts_for_training, checks=checks, provenance=provenance)


def store_episode(trajectory_store, run_id, episode_id, task_id, reward, steps=1, policy_version=3, parse_error=None):
    key = EpisodeKey(image_snapshot_digest="sha256:img", world_digest="sha256:world", task_digest="sha256:task",
                     run_id=run_id, seed=7, policy_version=policy_version)
    writer = trajectory_store.start_episode(episode(episode_id, run_id=run_id, task_id=task_id, key=key),
                                            INITIAL_STATE, {"total": 1250.0})
    for step_index in range(steps):
        prompt_ref = writer.put_prompt([{"role": "user", "content": f"Step {step_index}."}])
        observation = Observation.model_validate({"step_index": step_index, "screenshot": {
            "uri": "blob:f", "width": 1280, "height": 800, "sha256": f"{episode_id}_{step_index}"}})
        last = step_index == steps - 1
        writer.write_step(StepRecord(step_index=step_index, observation=observation,
                                     model_turn=ModelTurn(prompt_ref=prompt_ref, output_text="Action: wait()"),
                                     parse_error=parse_error if last else None))
    if reward is not None:
        writer.finish(reward, FINAL_STATE)


@pytest.fixture
def review_service():
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    store_episode(trajectory_store, "run_1", "episode_a", "task_000", scored(1.0, "success"), steps=2)
    store_episode(trajectory_store, "run_1", "episode_b", "task_000", scored(0.0, "failure", ["total"]), steps=2)
    store_episode(trajectory_store, "run_1", "episode_c", "task_001", scored(0.0, "agent_error"),
                  parse_error="no action found in model output")
    store_episode(trajectory_store, "run_1", "episode_d", "task_001", scored(0.0, "env_error",
                                                                              counts_for_training=False))
    store_episode(trajectory_store, "run_1", "episode_e", "task_000", scored(0.0, "failure"), policy_version=4)
    store_episode(trajectory_store, "run_1", "episode_running", "task_000", None)
    store_episode(trajectory_store, "run_2", "episode_f", "task_000", scored(0.0, "failure"))
    store_episode(trajectory_store, "run_2", "episode_g", "task_002", scored(1.0, "success"))
    labeler = LlmFailureLabeler(VerdictClient(), "judge_v1", b"nonce secret")
    return ReviewService(TrajectoryViews(trajectory_store), labeler, clock=float)


def test_failure_analysis_labels_clusters_and_files_tickets_once(review_service):
    analysis = review_service.analyze("run_1")
    assert {episode_id: label.category for episode_id, label in analysis.labels.items()} == {
        "episode_b": "verifier_false_negative", "episode_c": "adapter_parse", "episode_d": "env_issue",
        "episode_e": "verifier_false_negative"}
    assert {(cluster["policy_version"], cluster["category"]) for cluster in analysis.clusters} == {
        (3, "verifier_false_negative"), (3, "adapter_parse"), (3, "env_issue"), (4, "verifier_false_negative")}
    review_service.analyze("run_1")
    tickets = review_service.list_items(queue="verifier")
    assert len(tickets) == 2 and tickets[0].source == "failure_analysis"
    assert review_service.analysis("run_1").analyzed_at == 0.0


def test_cohorts_filter_and_carry_filmstrips(review_service):
    failures = review_service.cohort("run_1", EpisodeFilter(termination="failure"))
    assert [member.episode.episode_id for member in failures] == ["episode_b", "episode_e"]
    assert failures[0].frames == ["episode_b_0", "episode_b_1"]
    assert [member.episode.episode_id for member in review_service.cohort("run_1", EpisodeFilter(policy_version=4))] \
        == ["episode_e"]
    review_service.analyze("run_1")
    adapter = review_service.cohort("run_1", EpisodeFilter(category="adapter_parse"))
    assert [(member.episode.episode_id, member.category) for member in adapter] == [("episode_c", "adapter_parse")]
    assert len(review_service.cohort("run_1", EpisodeFilter(limit=2))) == 2


def test_provenance_and_prompts_explain_a_score(review_service):
    provenance = review_service.provenance("run_1", "episode_b")
    assert provenance.reward.checks[0].detail == "total differs"
    assert [(change.path, change.after) for change in provenance.world_diff] == [
        ("files/reconciliation.xlsx/cells/B2/value", 1200.0)]
    assert review_service.prompt("run_1", "episode_b", 1) == [{"role": "user", "content": "Step 1."}]
    with pytest.raises(UnknownRecordError):
        review_service.prompt("run_1", "episode_b", 5)


def test_reports_and_comparisons_use_shared_tasks(review_service):
    report = review_service.report("run_1")
    assert report["overall"]["episodes"] == 4 and report["excluded_env_errors"] == 1
    runs = review_service.compare_runs(["run_1", "run_2"])
    assert [row.task_id for row in runs.shared_tasks] == ["task_000"]
    assert runs.shared_tasks[0].success_rates == {"run_1": 1 / 3, "run_2": 0.0}
    versions = review_service.compare_policy_versions("run_1")
    assert list(versions.reports) == ["policy_version_3", "policy_version_4"]
    assert versions.shared_tasks[0].success_rates == {"policy_version_3": 0.5, "policy_version_4": 0.0}


def test_one_click_actions_file_review_items(review_service):
    flagged = review_service.act("run_1", "episode_b", ReviewRequest(action="flag", note="odd cursor jump"))
    assert (flagged.queue, flagged.note) == ("review", "odd cursor jump")
    bug = review_service.act("run_1", "episode_b", ReviewRequest(action="verifier_bug"))
    assert bug.queue == "verifier" and bug.title.endswith("task_000: total")
    draft = review_service.act("run_1", "episode_b", ReviewRequest(action="task_draft"))
    assert draft.payload["template"] == "tasks.finance.reconcile_invoices" and draft.payload["seed"] == 7
    with pytest.raises(ReviewError):
        review_service.act("run_1", "episode_b", ReviewRequest(action="label"))
    label = review_service.act("run_1", "episode_b", ReviewRequest(action="label", label={"verdict": "success"}))
    assert label.queue == "labeling"
    assert len(review_service.list_items(episode_id="episode_b")) == 4
    assert review_service.list_items(queue="audit") == []


def test_review_routes(review_service, example_payload, tmp_path):
    service = build_experiment_service(eval_payload(example_payload), tmp_path)
    client = create_app(service, review_service=review_service).test_client()
    cohort = client.get("/v1/runs/run_1/cohort?termination=failure&policy_version=3&limit=5").get_json()
    assert [member["episode"]["episode_id"] for member in cohort["episodes"]] == ["episode_b"]
    assert client.get("/v1/runs/run_1/cohort?limit=0").status_code == 400
    assert client.get("/v1/runs/run_1/episodes/episode_b/provenance").get_json()["world_diff"][0]["after"] == 1200.0
    assert client.get("/v1/runs/run_1/episodes/episode_b/steps/0/prompt").get_json()["messages"][0]["role"] == "user"
    assert client.get("/v1/runs/run_1/episodes/episode_b/steps/x/prompt").status_code == 400
    assert client.get("/v1/runs/run_1/failures").status_code == 404
    analysis = client.post("/v1/runs/run_1/failures/analyze").get_json()
    assert analysis["labels"]["episode_c"]["category"] == "adapter_parse"
    assert client.get("/v1/runs/run_1/failures").status_code == 200
    assert client.get("/v1/runs/run_1/report").get_json()["overall"]["episodes"] == 4
    assert client.get("/v1/compare/runs?run_ids=run_1").status_code == 400
    assert len(client.get("/v1/compare/runs?run_ids=run_1,run_2").get_json()["shared_tasks"]) == 1
    assert client.get("/v1/runs/run_1/compare/policy_versions").status_code == 200
    created = client.post("/v1/runs/run_1/episodes/episode_e/actions", json={"action": "audit_sample"})
    assert created.status_code == 201 and created.get_json()["queue"] == "audit"
    assert client.post("/v1/runs/run_1/episodes/episode_e/actions", json={"action": "label"}).status_code == 400
    assert len(client.get("/v1/review_items?queue=audit&status=open").get_json()["items"]) == 1
    assert create_app(service).test_client().get("/v1/review_items").status_code == 503
