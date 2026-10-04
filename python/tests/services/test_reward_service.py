import random

import pytest
from support.trajectory_fakes import episode

from rlworld.rewards import CheckResult, EpisodeOutcome, FunctionVerifier, VerifierRegistry
from rlworld.rewards.checks import cells_equal
from rlworld.sdk import UnknownLeaseError
from rlworld.services import EpisodeContext, RewardService
from rlworld.specs import parse_spec_document
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore

INITIAL_STATE = {"files": {"reconciliation.xlsx": {"cells": {"B2": {"value": None}}}}}
FINAL_STATE = {"files": {"reconciliation.xlsx": {"cells": {"B2": {"value": 1250.0}}}}}
GROUND_TRUTH = {"total": 1250.0}


class RecordingFleet:
    def __init__(self, call_log, final_state=None, finish_error=None, checkpoint_error=None, close_error=None):
        self.call_log = call_log
        self.final_state = final_state if final_state is not None else FINAL_STATE
        self.finish_error = finish_error
        self.checkpoint_error = checkpoint_error
        self.close_error = close_error

    def finish(self, lease_id, apps):
        self.call_log.append(f"fleet.finish {','.join(apps)}")
        if self.finish_error:
            raise self.finish_error
        return self.final_state

    def checkpoint(self, lease_id):
        self.call_log.append("fleet.checkpoint")
        if self.checkpoint_error:
            raise self.checkpoint_error
        return {"state_path": "/checkpoints/env_1.state", "memory_path": "/checkpoints/env_1.memory"}

    def close(self, lease_id):
        self.call_log.append("fleet.close")
        if self.close_error:
            raise self.close_error


class LoggingWriter:
    """Wraps a real trajectory writer to log when the record is written."""

    def __init__(self, writer, call_log):
        self.writer = writer
        self.call_log = call_log
        self.episode = writer.episode

    def finish(self, reward, final_state, final_snapshot_ref=None, retention="full"):
        self.call_log.append("writer.finish")
        return self.writer.finish(reward, final_state, final_snapshot_ref=final_snapshot_ref, retention=retention)


def check_total(context):
    cells = context.final_state["files"]["reconciliation.xlsx"]["cells"]
    return [cells_equal("total", cells, {"B2": context.ground_truth["total"]})]


def check_nothing(context):
    return [CheckResult(name="placeholder", tier="semantic", passed=True)]


def crash(context):
    raise KeyError("Summary")


def registry_with(exact_function):
    registry = VerifierRegistry()
    registry.register(FunctionVerifier("verifiers.excel.reconciliation_values_match", "1", "exact", exact_function))
    registry.register(FunctionVerifier("verifiers.mail.summary_lists_discrepant_invoices", "1", "semantic",
                                       check_nothing))
    return registry


@pytest.fixture
def context(example_payload):
    task = parse_spec_document(example_payload).task.model_copy(update={"judge_rubric": None})
    return EpisodeContext(lease_id="lease_00000001", task=task, apps=("files",), initial_state=INITIAL_STATE,
                          ground_truth=GROUND_TRUTH, instruction="Reconcile.")


def started_writer(call_log):
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    writer = trajectory_store.start_episode(episode(), INITIAL_STATE, GROUND_TRUTH)
    return trajectory_store, LoggingWriter(writer, call_log)


def test_scores_records_then_tears_down(context):
    call_log = []
    trajectory_store, writer = started_writer(call_log)
    service = RewardService(RecordingFleet(call_log), registry_with(check_total), audit_snapshot_rate=1.0,
                            random_generator=random.Random(0))
    reward = service.finish_episode(context, EpisodeOutcome(steps_taken=12, final_action="done"), writer)
    assert reward.termination == "success"
    assert call_log == ["fleet.finish files", "fleet.checkpoint", "writer.finish", "fleet.close"]
    stored = trajectory_store.load_episode("run_1", "episode_1")
    assert stored.reward == reward
    assert stored.final_snapshot_ref == "/checkpoints/env_1.state"
    assert trajectory_store.load_state(stored.final_state_ref) == FINAL_STATE


def test_a_failed_finish_is_an_env_error_and_still_closes(context):
    call_log = []
    trajectory_store, writer = started_writer(call_log)
    fleet = RecordingFleet(call_log, finish_error=TimeoutError("guest agent silent"))
    reward = RewardService(fleet, registry_with(check_total)).finish_episode(
        context, EpisodeOutcome(steps_taken=3, final_action="done"), writer)
    assert (reward.termination, reward.counts_for_training) == ("env_error", False)
    assert "guest agent silent" in reward.error_detail
    assert call_log[-2:] == ["writer.finish", "fleet.close"]
    assert trajectory_store.load_episode("run_1", "episode_1").retention == "full"


def test_crashing_verifiers_and_bad_config_become_env_errors(context):
    crashed = RewardService(RecordingFleet([]), registry_with(crash)).finish_episode(
        context, EpisodeOutcome(steps_taken=3, final_action="done"))
    assert crashed.termination == "env_error"
    misconfigured = RewardService(RecordingFleet([]), VerifierRegistry()).finish_episode(
        context, EpisodeOutcome(steps_taken=3, final_action="done"))
    assert misconfigured.termination == "env_error"
    assert "UnknownVerifierError" in misconfigured.error_detail


def test_audit_snapshots_are_sampled_and_optional(context):
    call_log = []
    RewardService(RecordingFleet(call_log), registry_with(check_total), audit_snapshot_rate=0.0).finish_episode(
        context, EpisodeOutcome(steps_taken=3, final_action="done"))
    assert "fleet.checkpoint" not in call_log
    fleet = RecordingFleet([], checkpoint_error=RuntimeError("disk full"))
    reward = RewardService(fleet, registry_with(check_total), audit_snapshot_rate=1.0).finish_episode(
        context, EpisodeOutcome(steps_taken=3, final_action="done"))
    assert reward.termination == "success"


def test_an_already_expired_lease_does_not_break_scoring(context):
    fleet = RecordingFleet([], close_error=UnknownLeaseError(404, "unknown_lease", "gone"))
    reward = RewardService(fleet, registry_with(check_total)).finish_episode(
        context, EpisodeOutcome(steps_taken=3, final_action="done"))
    assert reward.termination == "success"


def test_successes_keep_thumbnails_unless_sampled(context):
    call_log = []
    trajectory_store, writer = started_writer(call_log)
    service = RewardService(RecordingFleet(call_log), registry_with(check_total), full_retention_rate=0.0,
                            random_generator=random.Random(1))
    service.finish_episode(context, EpisodeOutcome(steps_taken=3, final_action="done"), writer)
    assert trajectory_store.load_episode("run_1", "episode_1").retention == "thumbnail"


def test_what_the_world_changed_mid_episode_is_never_a_side_effect(context):
    inbox_before = {**INITIAL_STATE, "mail": {"inbox": []}}
    arrived = {**FINAL_STATE, "mail": {"inbox": [{"id": "mail_event_cfo_nudge", "subject": "Where are we?"}]}}
    world_context = EpisodeContext(lease_id=context.lease_id, task=context.task, apps=("files", "mail"),
                                   initial_state=inbox_before, ground_truth=GROUND_TRUTH,
                                   world_paths=("mail/inbox/mail_event_cfo_nudge",))
    service = RewardService(RecordingFleet([], final_state=arrived), registry_with(check_total))
    outcome = EpisodeOutcome(steps_taken=12, final_action="done")
    assert service.finish_episode(world_context, outcome).termination == "success"
    unexplained = EpisodeContext(lease_id=context.lease_id, task=context.task, apps=("files", "mail"),
                                 initial_state=inbox_before, ground_truth=GROUND_TRUTH)
    reward = service.finish_episode(unexplained, outcome)
    assert reward.termination == "failure"
    assert [change.path for change in reward.side_effect_violations] == ["mail/inbox/mail_event_cfo_nudge"]
