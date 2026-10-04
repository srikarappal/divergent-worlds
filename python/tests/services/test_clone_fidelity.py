import pytest
from support.experiment_fakes import bridged_http_client, build_experiment_service, eval_payload
from support.trajectory_fakes import episode

from rlworld.rewards.records import Provenance, RewardPolicy, RewardRecord
from rlworld.sdk import ExperimentApiError, ExperimentClient
from rlworld.services.review import ReviewError, ReviewService
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore


def reward(termination):
    total = 1.0 if termination == "success" else 0.0
    provenance = Provenance(state_hash="sha256:s", ground_truth_digest="sha256:g", reward_policy=RewardPolicy())
    return RewardRecord(total=total, components={"outcome": total}, termination=termination,
                        counts_for_training=termination != "env_error", provenance=provenance)


def store(trajectory_store, run_id, task_id, termination, number, run_type="eval"):
    record = episode(f"{run_id}_{number:03d}", run_id=run_id, task_id=task_id, run_type=run_type)
    trajectory_store.start_episode(record, {}, {}).finish(reward(termination), {})


@pytest.fixture
def review_service():
    """The clone solves twelve of twenty shared tasks, the real app eight; the clone also has a task of its own."""
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    outcomes = [("success", "success")] * 7 + [("success", "failure")] * 5 + [("failure", "success")]
    outcomes += [("failure", "failure")] * 7
    for number, (on_clone, on_real) in enumerate(outcomes):
        store(trajectory_store, "run_clone", f"task_{number:03d}", on_clone, number)
        store(trajectory_store, "run_real", f"task_{number:03d}", on_real, number)
    store(trajectory_store, "run_clone", "task_extra", "success", 99)
    store(trajectory_store, "run_real", "task_000", "env_error", 98)
    store(trajectory_store, "run_train", "task_000", "success", 0, run_type="rl")
    return ReviewService(TrajectoryViews(trajectory_store), clock=float)


def test_a_clone_run_against_a_real_app_run_over_their_shared_tasks(review_service):
    report = review_service.clone_fidelity("crm", "run_clone", "run_real")
    assert (report.app, report.tasks, report.clone_only, report.real_only) == ("crm", 20, 5, 1)
    assert (report.clone_success_rate, report.real_success_rate) == (0.6, 0.4)
    assert round(report.gap, 6) == 0.2 and round(report.fidelity, 6) == 0.8 and report.agreement == 0.7
    assert round(report.p_value, 4) == 0.2188 and not report.large_gap


@pytest.mark.parametrize("clone_run_id, real_run_id, message", [
    ("run_clone", "run_clone", "a different run"),
    ("run_train", "run_real", "eval slice only and run_train is not an eval run"),
    ("run_clone", "run_nothing", "share no scored task"),
])
def test_fidelity_needs_two_eval_runs_with_shared_tasks(review_service, clone_run_id, real_run_id, message):
    with pytest.raises(ReviewError, match=message):
        review_service.clone_fidelity("crm", clone_run_id, real_run_id)


def test_the_sdk_and_route_measure_fidelity(review_service, example_payload, tmp_path):
    service = build_experiment_service(eval_payload(example_payload), tmp_path)
    client = ExperimentClient("http://experimentd.internal",
                              bridged_http_client(service, review_service=review_service))
    report = client.clone_fidelity("crm", "run_clone", "run_real")
    assert (report.app, report.tasks, report.clone_only) == ("crm", 20, 5)
    with pytest.raises(ExperimentApiError) as refused:
        client.clone_fidelity("crm", "run_train", "run_real")
    assert refused.value.status_code == 400 and "eval slice only" in str(refused.value.problems)
    with pytest.raises(ExperimentApiError) as missing:
        client.request("GET", "/v1/compare/fidelity", params={"app": "crm", "clone_run_id": "run_clone"})
    assert missing.value.problems == ["real_run_id: required"]
