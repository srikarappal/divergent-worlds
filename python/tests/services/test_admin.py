import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from support.experiment_fakes import build_experiment_service, eval_payload
from support.gateway_fakes import FakeUpstream, build_gateway, chat_request, completion
from support.trajectory_fakes import episode

from rlworld.actions.models import Observation
from rlworld.gateway import DataPolicyViolationError
from rlworld.rewards.records import CheckResult, Provenance, RewardPolicy, RewardRecord
from rlworld.services.admin import DataPolicy, DataPolicyStore, PlatformAuditLog
from rlworld.services.experimentd_http import create_app
from rlworld.services.retention import RetentionSweeper
from rlworld.services.review import FiledItem, ReviewError, ReviewRequest, ReviewService, ReviewUpdate
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.trajectories import InMemoryObjectStore, ModelTurn, StepRecord, TrajectoryStore
from rlworld.trajectories.object_store import blob_key, put_blob

NOW = datetime(2027, 1, 1, tzinfo=timezone.utc)
SCREENSHOT_MESSAGE = {"role": "user", "content": [{"type": "text", "text": "Step 0."},
                                                  {"type": "image", "uri": "https://frames.example/1.webp"}]}


def png(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1280, 800), color).save(output, format="PNG")
    return output.getvalue()


def scored(termination: str, judge_passed: bool | None = None) -> RewardRecord:
    checks = [CheckResult(name="total", tier="exact", passed=termination == "success")]
    if judge_passed is not None:
        checks.append(CheckResult(name="tone", tier="judge", passed=judge_passed))
    provenance = Provenance(state_hash="sha256:s", ground_truth_digest="sha256:g", reward_policy=RewardPolicy())
    return RewardRecord(total=1.0 if termination == "success" else 0.0, components={}, termination=termination,
                        counts_for_training=True, checks=tuple(checks), provenance=provenance)


def store_episode(trajectory_store, run_id, episode_id, termination="success", frame=None, finished_days_ago=None,
                  judge_passed=None):
    writer = trajectory_store.start_episode(episode(episode_id, run_id=run_id), {"cells": {}}, {"total": 1})
    observation = Observation.model_validate({"step_index": 0, "screenshot": {
        "uri": "blob:f", "width": 1280, "height": 800, "sha256": frame}} if frame else {"step_index": 0})
    prompt_ref = writer.put_prompt([{"role": "user", "content": f"prompt for {episode_id}"}])
    writer.write_step(StepRecord(step_index=0, observation=observation,
                                 model_turn=ModelTurn(prompt_ref=prompt_ref, output_text="Action: finished()")))
    if finished_days_ago is None:
        return writer
    finished = writer.finish(scored(termination, judge_passed), {"cells": {"B2": episode_id}})
    finished_at = (NOW - timedelta(days=finished_days_ago)).isoformat()
    trajectory_store.save_episode(finished.model_copy(update={"finished_at": finished_at}))
    return finished


class AllowListPolicy:
    def allows_screenshots(self, run_id, endpoint_name):
        return run_id == "run_trusted"


def test_the_gateway_asks_the_tenant_policy_before_sending_frames():
    upstream = FakeUpstream([completion()])
    gateway, _ = build_gateway(upstream, screenshot_policy=AllowListPolicy())
    with pytest.raises(DataPolicyViolationError):
        gateway.complete(chat_request(messages=(SCREENSHOT_MESSAGE,), run_id="run_other"))
    assert gateway.complete(chat_request(messages=(SCREENSHOT_MESSAGE,), run_id="run_trusted")).text == "OK"
    assert len(upstream.requests) == 1


def test_data_policies_default_per_tenant_and_check_their_windows():
    tenants = {"run_a": "finance_lab"}
    policies = DataPolicyStore(tenants.get)
    assert policies.allows_screenshots("run_a", "frontier") and policies.allows_export("run_a", "sft")
    policies.set(DataPolicy(tenant="finance_lab", screenshot_endpoints=["policy_vllm"], exports=["preferences"]))
    assert not policies.allows_screenshots("run_a", "frontier") and policies.allows_screenshots("run_a", "policy_vllm")
    assert not policies.allows_export("run_a", "sft")
    assert [policy.tenant for policy in policies.all()] == ["default", "finance_lab"]
    with pytest.raises(ValueError, match="outlive"):
        DataPolicy(tenant="x", trajectory_days=10, full_frame_days=30)


def test_the_audit_log_filters_newest_first():
    audit = PlatformAuditLog(clock=float)
    audit.record("user_ada", "lab", "launch", {"digest": "sha256:a"}, {})
    audit.record("user_bob", "lab", "control", {"run_id": "r"}, {"command": "stop"})
    audit.record("user_ada", "lab", "control", {"run_id": "r"}, {"command": "pause"})
    assert [record.sequence for record in audit.query()] == [3, 2, 1]
    assert [record.detail["command"] for record in audit.query(action="control")] == ["pause", "stop"]
    assert [record.sequence for record in audit.query(actor="user_ada", limit=1)] == [3]


@pytest.fixture
def review_service():
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    store_episode(trajectory_store, "run_1", "episode_pass", "success", finished_days_ago=1, judge_passed=True)
    store_episode(trajectory_store, "run_1", "episode_fail", "failure", finished_days_ago=1, judge_passed=False)
    store_episode(trajectory_store, "run_1", "episode_miss", "failure", finished_days_ago=1, judge_passed=True)
    return ReviewService(TrajectoryViews(trajectory_store), clock=float)


def test_labelers_resolve_items_and_labels_calibrate_verifiers_and_judges(review_service):
    labels = {"episode_pass": {"verdict": "success", "items": {"tone": True}},
              "episode_fail": {"verdict": "failure", "items": {"tone": False}},
              "episode_miss": {"verdict": "success", "items": {"tone": False}}}
    for episode_id, label in labels.items():
        item = review_service.act("run_1", episode_id, ReviewRequest(action="label", label={"verdict": "failure"}))
        review_service.update_item(item.item_id, ReviewUpdate(status="resolved", label=label), "labeler_kim")
    resolved = review_service.list_items(queue="labeling", status="resolved")
    assert len(resolved) == 3 and resolved[0].resolved_by == "labeler_kim"
    report = review_service.calibration("run_1")
    assert (report.verifier.labels, report.verifier.precision, report.verifier.recall) == (3, 1.0, 0.5)
    assert report.verifier.false_fails == 1
    assert report.judge["tone"]["false_passes"] == 1 and report.judge["overall"]["label_count"] == 3
    assert any("need 300" in problem for problem in report.judge_problems)
    flag = review_service.act("run_1", "episode_fail", ReviewRequest(action="flag"))
    with pytest.raises(ReviewError, match="only labeling items"):
        review_service.update_item(flag.item_id, ReviewUpdate(label={"verdict": "success"}), "labeler_kim")
    filed = review_service.file_item(FiledItem(source="gates", title="task_017 fails the near miss gate"), "gates_job")
    assert (filed.queue, filed.action, filed.created_by) == ("review", "gate_failure", "gates_job")


def test_exports_follow_the_data_policy_and_queue_routes(review_service, example_payload, tmp_path):
    service = build_experiment_service(eval_payload(example_payload), tmp_path)
    policies = DataPolicyStore()
    client = create_app(service, trajectory_views=review_service.views, review_service=review_service,
                        data_policy_store=policies).test_client()
    assert [row["episode_id"] for row in client.get("/v1/runs/run_1/exports/sft").get_json()["rows"]] == [
        "episode_pass"]
    assert client.get("/v1/runs/run_1/exports/frames").status_code == 400
    policies.set(DataPolicy(tenant="default", exports=["preferences"]))
    assert client.get("/v1/runs/run_1/exports/sft").status_code == 403
    filed = client.post("/v1/review_items", json={"source": "alert", "title": "reward spike on task_000"})
    assert filed.status_code == 201
    item_id = filed.get_json()["item_id"]
    resolved = client.patch(f"/v1/review_items/{item_id}", json={"status": "dismissed", "note": "known flake"})
    assert resolved.get_json()["status"] == "dismissed" and resolved.get_json()["resolved_by"] == "developer"
    assert client.patch(f"/v1/review_items/{item_id}", json={"status": "done"}).status_code == 400
    assert client.patch("/v1/review_items/review_999999", json={"status": "resolved"}).status_code == 404
    assert client.get("/v1/calibration?run_id=run_1").get_json()["verifier"]["labels"] == 0
    assert client.put("/v1/admin/data_policies/finance_lab",
                      json={"trajectory_days": 7, "full_frame_days": 3}).get_json()["full_frame_days"] == 3
    assert client.put("/v1/admin/data_policies/finance_lab", json={"full_frame_days": 900}).status_code == 400
    assert client.get("/v1/admin/pools").status_code == 503


class FakePools:
    def availability(self):
        return {"office_ltsc": 7, "identity:finance_manager": 2}


def test_pool_status_and_retention_routes(example_payload, tmp_path):
    service = build_experiment_service(eval_payload(example_payload), tmp_path)
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    views = TrajectoryViews(trajectory_store)
    sweeper = RetentionSweeper(views, DataPolicyStore(), now=NOW.astimezone)
    client = create_app(service, trajectory_views=views, pool_status_source=FakePools(),
                        retention_sweeper=sweeper).test_client()
    assert client.get("/v1/admin/pools").get_json()["pools"]["office_ltsc"] == 7
    assert client.post("/v1/admin/retention/sweep").get_json() == {
        "deleted_episodes": [], "thumbnail_only_frames": 0, "deleted_blobs": 0}


def test_retention_deletes_expired_episodes_and_keeps_thumbnails_of_aged_frames():
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    object_store = trajectory_store.object_store
    shared = put_blob(object_store, png("navy")).removeprefix("sha256:")
    aged_only = put_blob(object_store, png("teal")).removeprefix("sha256:")
    expired_only = put_blob(object_store, png("maroon")).removeprefix("sha256:")
    expired = store_episode(trajectory_store, "run_1", "episode_expired", frame=expired_only, finished_days_ago=400)
    store_episode(trajectory_store, "run_1", "episode_shared", frame=shared, finished_days_ago=500)
    aged = store_episode(trajectory_store, "run_1", "episode_aged", frame=aged_only, finished_days_ago=60)
    store_episode(trajectory_store, "run_1", "episode_fresh", frame=shared, finished_days_ago=2)
    store_episode(trajectory_store, "run_1", "episode_running", frame=shared)
    views = TrajectoryViews(trajectory_store)
    report = RetentionSweeper(views, DataPolicyStore(), now=NOW.astimezone).sweep()
    assert sorted(report.deleted_episodes) == ["run_1/episode_expired", "run_1/episode_shared"]
    assert report.thumbnail_only_frames == 1
    assert trajectory_store.list_episode_ids("run_1") == ["episode_aged", "episode_fresh", "episode_running"]
    assert not object_store.exists(blob_key(expired_only))
    assert not object_store.exists(blob_key(expired.final_state_ref))
    assert object_store.exists(blob_key(expired.initial_state_ref))
    assert object_store.exists(blob_key(shared))
    assert not object_store.exists(blob_key(aged_only))
    thumbnail, media_type = views.frame(aged_only, "full")
    assert media_type == "image/webp" and Image.open(io.BytesIO(thumbnail)).size == (320, 200)
    assert object_store.exists(blob_key(aged.final_state_ref))
