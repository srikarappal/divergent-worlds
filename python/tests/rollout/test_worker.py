import threading

import pytest
from support.rollout_fakes import FakeFleet, FakeTaskProvider, ScriptedGateway, build_group_runner, rollout_document

from rlworld.gateway import UsageMeter
from rlworld.preflight import PreflightContext, SeatPool
from rlworld.rollout import ConcurrencyGate, PlatformRunExecutor, RolloutWorker
from rlworld.services.experiments import ExperimentService, RunRecord
from rlworld.services.trainer_launch import TrainerLauncher
from rlworld.specs import dump_spec_document, spec_digest
from rlworld.tasks import GateReport
from rlworld.tasks.curriculum import difficulty_key
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeControl:
    """Serves desired states in order and records what the worker reports."""

    def __init__(self, states=("running",), priority="training", concurrency=8):
        self.states = list(states)
        self.priority = priority
        self.concurrency = concurrency
        self.polls = 0
        self.spend = []
        self.metrics = []
        self.finished = []

    def run(self, run_id):
        state = self.states[min(self.polls, len(self.states) - 1)]
        self.polls += 1
        return run_record(run_id, state, self.priority, self.concurrency)

    def record_spend(self, run_id, amount_usd):
        self.spend.append(amount_usd)

    def publish_metrics(self, run_id, metrics):
        self.metrics.append(metrics)

    def mark_finished(self, run_id, state, detail=""):
        self.finished.append((state, detail))


class NoRunner:
    def run(self, argv, working_directory):
        raise AssertionError("eval runs never submit trainer jobs")


class WorkerFactory:
    """Builds platform hosted workers; experimentd itself is the control, wired in after construction."""

    def __init__(self, fleet, gateway, trajectory_store):
        self.fleet = fleet
        self.gateway = gateway
        self.trajectory_store = trajectory_store
        self.control = None

    def __call__(self, run_id, document):
        group_runner = build_group_runner(document, self.fleet, self.gateway, self.trajectory_store, run_id=run_id)
        return RolloutWorker(run_id, document, group_runner, FakeTaskProvider(document, 2), control=self.control,
                             poll_seconds=0.01)


def run_record(run_id, state, priority="training", concurrency=8):
    return RunRecord(run_id=run_id, experiment_id="experiment_000001", run_name="point_000", spec_digest="sha256:s",
                     run_digest="sha256:r", run_type="rl", assignments={}, state=state,
                     desired_concurrency=concurrency, priority=priority, created_at=0.0)


def eval_document(example_payload, **run_updates):
    policy = {**example_payload["run"]["policy"], "token_ids": False, "logprobs": False}
    return rollout_document(example_payload, {"type": "eval", "policy": policy, **run_updates}, {"split": "eval"})


def build_worker(document, fleet=None, gateway=None, control=None, task_count=3, meter=None, fake_time=None):
    fake_time = fake_time or FakeTime()
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    group_runner = build_group_runner(document, fleet or FakeFleet(), gateway or ScriptedGateway(), trajectory_store)
    provider = FakeTaskProvider(document, task_count)
    worker = RolloutWorker("run_1", document, group_runner, provider, control=control, meter=meter,
                           poll_seconds=1.0, clock=fake_time.time, sleep=fake_time.sleep)
    return worker, provider, trajectory_store


@pytest.fixture
def document(example_payload):
    return rollout_document(example_payload)


def test_rl_batches_sample_tasks_and_keep_informative_groups(document):
    worker, _, _ = build_worker(document)
    batch = worker.collect_batch(policy_version=3)
    assert sorted(group.group_id for group in batch.groups) == ["b00000_g00000", "b00000_g00001"]
    assert len(batch.informative_groups) == 2 and batch.zero_advantage_groups == 0
    assert {episode.key.policy_version for group in batch.groups for episode in group.episodes} == {3}
    tracker = worker.sampler.tracker
    assert tracker.observations(document.task.generator, difficulty_key(document.task.difficulty), 3) == 8
    assert worker.collect_batch(policy_version=4).batch_index == 1


def test_groups_without_advantage_are_dropped(document):
    worker, _, _ = build_worker(document, gateway=ScriptedGateway(scripts={}))
    batch = worker.collect_batch()
    assert batch.informative_groups == () and batch.zero_advantage_groups == 2


def test_eval_runs_play_the_suite_in_order_and_complete(example_payload):
    control = FakeControl()
    worker, provider, trajectory_store = build_worker(eval_document(example_payload), control=control)
    worker.run()
    assert sorted(provider.instanced) == ["task_000", "task_001", "task_002"]
    episode_ids = trajectory_store.list_episode_ids("run_1")
    assert len(episode_ids) == 12 and "b00001_g00000_s003_a0" in episode_ids
    assert control.finished == [("completed", "")]
    assert control.metrics[-1]["episodes"] == 12 and control.metrics[-1]["success_rate"] == 0.25


def test_paused_runs_wait_and_priority_changes_reach_new_leases(document):
    fleet = FakeFleet()
    fake_time = FakeTime()
    control = FakeControl(states=("paused", "paused", "running"), priority="eval")
    worker, _, _ = build_worker(document, fleet=fleet, control=control, fake_time=fake_time)
    batch = worker.collect_batch()
    assert len(batch.groups) == 2 and fake_time.sleeps[:2] == [1.0, 1.0]
    assert {call[2] for call in fleet.calls_named("lease_group")} == {"eval"}


def test_stopped_runs_start_nothing_and_are_not_marked_complete(example_payload):
    control = FakeControl(states=("stopped",))
    worker, provider, _ = build_worker(eval_document(example_payload), control=control)
    worker.run()
    assert provider.instanced == [] and control.finished == []


def test_the_gate_bounds_envs_in_flight():
    gate = ConcurrencyGate(8)
    assert gate.try_acquire(4, 0) and gate.try_acquire(4, 0)
    assert not gate.try_acquire(4, 0)
    gate.release(4)
    gate.set_limit(4)
    assert not gate.try_acquire(4, 0)
    gate.release(4)
    assert gate.try_acquire(16, 0)


def test_spend_is_reported_once_per_dollar(document):
    meter = UsageMeter()
    control = FakeControl()
    gateway = ScriptedGateway(meter=meter, cost_usd=0.25)
    worker, _, _ = build_worker(document, gateway=gateway, control=control, meter=meter)
    worker.collect_batch()
    assert sum(control.spend) == pytest.approx(0.25 * len(gateway.requests))
    reports = len(control.spend)
    worker.report_spend()
    assert len(control.spend) == reports


def test_the_platform_executor_runs_eval_runs_and_dry_runs(example_payload, tmp_path):
    document = eval_document(example_payload, batches=1)
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    factory = WorkerFactory(FakeFleet(), ScriptedGateway(), trajectory_store)
    executor = PlatformRunExecutor(factory)
    context = PreflightContext(
        gate_reports={spec_digest(document.task): GateReport(spec_digest(document.task), ())},
        seat_pools={"windows_server_dc": SeatPool(1000, "per_host"), "office_ltsc": SeatPool(10_000)},
    )
    service = ExperimentService(context, executor, TrainerLauncher({}, NoRunner(), tmp_path), b"run token secret",
                                "http://fleetd.internal", "http://experimentd.internal")
    factory.control = service
    digest = service.save_spec(dump_spec_document(document))
    run_id = service.launch(digest).run_ids[0]
    assert executor.wait(run_id, timeout_seconds=10)
    assert service.run(run_id).state == "completed"
    assert any(event.kind == "metrics" for event in service.run_events(run_id))
    assert len(trajectory_store.list_episode_ids(run_id)) == 8
    episode_id = service.dry_run(digest)
    assert episode_id == "dry_run_000001_s000_a0"
    dry_run_thread = next(thread for thread in threading.enumerate() if thread.name == "rollout_dry_run_000001")
    dry_run_thread.join(10)
    assert trajectory_store.load_episode("dry_run_000001", episode_id).reward.termination == "success"
