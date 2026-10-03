import pytest
from support.rollout_fakes import (
    INSTRUCTION,
    SNAPSHOT,
    SOLVING_REPLIES,
    FakeFleet,
    ScriptedGateway,
    build_episode_runner,
    rollout_document,
    task_instance,
)

from rlworld.gateway import RateLimitTimeoutError
from rlworld.gateway.messages import ChatResponse
from rlworld.rollout import EpisodePlan
from rlworld.rollout.episode import NO_USER_REPLY
from rlworld.sdk import UnknownLeaseError
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore


class RecordingSimulator:
    def __init__(self, reply="Only March."):
        self.reply_text = reply
        self.questions = []

    def reply(self, persona, hidden_intent, instruction, question):
        self.questions.append((persona, hidden_intent, instruction, question))
        return self.reply_text


class RecordingEntitlements:
    def __init__(self):
        self.renewed = []

    def renew(self, lease_id):
        self.renewed.append(lease_id)


class TickingGateway:
    """Each policy call takes fifteen seconds of fake time."""

    def __init__(self, gateway, clock):
        self.gateway = gateway
        self.clock = clock

    def complete(self, request):
        self.clock.now += 15.0
        return self.gateway.complete(request)


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now


class ExecGateway:
    def complete(self, request):
        return ChatResponse(text="", tool_calls=({"name": "exec", "arguments": '{"command": "ls"}'},),
                            served_model=request.model)


class ExpiredLeaseFleet(FakeFleet):
    def heartbeat(self, lease_id):
        raise UnknownLeaseError(404, "unknown_lease", "lease expired")


def run_episode(document, fleet, gateway, entitlement_lease_id=None, **options):
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    runner = build_episode_runner(document, fleet, gateway, trajectory_store, **options)
    lease = fleet.open_lease()
    plan = EpisodePlan("episode_1", task_instance(document), policy_version=3, group_id="group_1")
    return runner.run(plan, lease, entitlement_lease_id), trajectory_store, lease


@pytest.fixture
def document(example_payload):
    return rollout_document(example_payload)


def test_a_solved_episode_records_every_turn(document):
    fleet = FakeFleet()
    episode, trajectory_store, lease = run_episode(document, fleet, ScriptedGateway())
    assert episode.reward.termination == "success" and episode.reward.total == 1.0
    assert episode.key.policy_version == 3 and episode.key.image_snapshot_digest == SNAPSHOT.digest
    steps = trajectory_store.load_steps("run_1", "episode_1")
    assert [step.actions for step in steps] == [({"name": "type", "text": "1250"},), ({"name": "done"},)]
    assert [step.observation.step_index for step in steps] == [0, 1]
    assert steps[0].model_turn.token_trace.completion_token_ids == (7, 8)
    assert (steps[0].timings.model_seconds, steps[0].timings.env_seconds) == (1.5, 0.4)
    second_prompt = trajectory_store.load_json_blob(steps[1].model_turn.prompt_ref)
    assert second_prompt[2] == {"role": "assistant", "content": SOLVING_REPLIES[0]}
    assert [call[1:] for call in fleet.calls] == [
        (lease.lease_id, "wait"), (lease.lease_id, "type"), (lease.lease_id, ("files",)), (lease.lease_id,)]
    assert trajectory_store.load_episode("run_1", "episode_1", with_steps=False).finished_at is not None


def test_unparseable_replies_are_agent_errors(document):
    fleet = FakeFleet()
    episode, trajectory_store, _ = run_episode(document, fleet, ScriptedGateway({0: ("I am not sure what to do",)}))
    assert episode.reward.termination == "agent_error" and episode.reward.counts_for_training
    assert trajectory_store.load_steps("run_1", "episode_1")[-1].parse_error
    assert fleet.calls_named("close")


def test_disallowed_modalities_are_agent_errors(example_payload):
    policy = {**example_payload["run"]["policy"], "action_format": "computer_tool"}
    document = rollout_document(example_payload, {"policy": policy})
    episode, trajectory_store, _ = run_episode(document, FakeFleet(), ExecGateway())
    assert episode.reward.termination == "agent_error"
    assert "outside the allowed modalities" in trajectory_store.load_steps("run_1", "episode_1")[0].parse_error


def test_policy_failures_and_crashed_envs_are_env_errors(document):
    fleet = FakeFleet()
    episode, _, _ = run_episode(document, fleet, ScriptedGateway(failure=RateLimitTimeoutError("busy")))
    assert episode.reward.termination == "env_error" and not episode.reward.counts_for_training
    assert "policy call failed" in episode.reward.error_detail
    crashing_fleet = FakeFleet(crashing_leases={"lease_0001"})
    crashed, trajectory_store, _ = run_episode(document, crashing_fleet, ScriptedGateway())
    assert crashed.reward.termination == "env_error"
    assert trajectory_store.load_steps("run_1", "episode_1") == ()
    assert crashing_fleet.open_leases == set()


def test_the_step_budget_ends_in_max_steps(document):
    fleet = FakeFleet()
    episode, trajectory_store, _ = run_episode(document, fleet, ScriptedGateway({0: ("Action: wait()",)}))
    assert episode.reward.termination == "max_steps"
    assert len(trajectory_store.load_steps("run_1", "episode_1")) == document.task.max_steps
    assert len(fleet.calls_named("step")) == 1 + document.task.max_steps


def test_ask_user_goes_to_the_simulator_and_never_to_the_env(document):
    fleet = FakeFleet()
    simulator = RecordingSimulator()
    replies = ("Thought: Which month counts?\nAction: call_user()", "Action: finished()")
    episode, trajectory_store, _ = run_episode(document, fleet, ScriptedGateway({0: replies}),
                                               user_simulator=simulator)
    assert simulator.questions == [("finance_manager", "Only March invoices count.", INSTRUCTION,
                                    "Which month counts?")]
    steps = trajectory_store.load_steps("run_1", "episode_1")
    assert steps[1].observation.user_reply == "Only March."
    assert [call[2] for call in fleet.calls_named("step")] == ["wait"]
    assert episode.reward.termination == "failure"


def test_ask_user_without_a_simulator(example_payload, document):
    plain_task = {"user_simulator": None, "difficulty": {**example_payload["task"]["difficulty"], "ambiguity": "none"}}
    replies = ("Thought: Which month?\nAction: call_user()", "Action: finished()")
    unassisted, trajectory_store, _ = run_episode(rollout_document(example_payload, task_updates=plain_task),
                                                  FakeFleet(), ScriptedGateway({0: replies}))
    assert trajectory_store.load_steps("run_1", "episode_1")[1].observation.user_reply == NO_USER_REPLY
    assert unassisted.reward.termination == "failure"
    misconfigured, _, _ = run_episode(document, FakeFleet(), ScriptedGateway({0: replies}))
    assert misconfigured.reward.termination == "env_error"


def test_leases_and_entitlements_are_renewed_on_their_interval(document):
    fake_time = FakeTime()
    fleet = FakeFleet()
    entitlements = RecordingEntitlements()
    episode, _, lease = run_episode(document, fleet, TickingGateway(ScriptedGateway(), fake_time), "entitlement_1",
                                    entitlements=entitlements, heartbeat_seconds=10.0, clock=fake_time.time)
    assert episode.reward.termination == "success"
    assert fleet.calls_named("heartbeat") == [("heartbeat", lease.lease_id)]
    assert entitlements.renewed == ["entitlement_1"]
    expired, _, _ = run_episode(document, ExpiredLeaseFleet(), TickingGateway(ScriptedGateway(), fake_time),
                                heartbeat_seconds=10.0, clock=fake_time.time)
    assert expired.reward.termination == "env_error" and "lease renewal failed" in expired.reward.error_detail


def test_episodes_without_an_env_are_recorded_as_env_errors(document):
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    runner = build_episode_runner(document, FakeFleet(), ScriptedGateway(), trajectory_store)
    plan = EpisodePlan("episode_2", task_instance(document), policy_version=0, group_id="group_1", sample_index=2)
    episode = runner.record_env_error(plan, "fork failed")
    stored = trajectory_store.load_episode("run_1", "episode_2")
    assert stored.reward.termination == "env_error" and stored.reward.error_detail == "fork failed"
    assert (episode.sample_index, stored.steps) == (2, ())
