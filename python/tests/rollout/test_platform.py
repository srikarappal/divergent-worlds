import copy
import time

import pytest
from support.rollout_fakes import (
    INITIAL_STATE,
    SOLVED_STATE,
    SOLVING_REPLIES,
    FakeFleet,
    ScriptedGateway,
    rollout_document,
    task_instance,
    verifier_registry,
)

from rlworld.entitlements import (
    EntitlementBroker,
    EphemeralSecretIssuer,
    IdentityPoolConfig,
    SeatPoolConfig,
    SignedSessionMinter,
)
from rlworld.gateway import UsageMeter
from rlworld.judge.judge import RubricJudge
from rlworld.judge.rubric import RubricLibrary
from rlworld.rewards.records import Provenance, RewardPolicy, RewardRecord
from rlworld.rollout.groups import EnvShape
from rlworld.rollout.platform import PlatformSolvabilityProbe, PlatformWorkers, SimulatorEndpoint, episode_outcome
from rlworld.rollout.user_simulator import GatewayNpcSimulator, GatewayUserSimulator
from rlworld.sdk.client import SnapshotSource
from rlworld.specs import parse_spec_document, spec_digest
from rlworld.tasks.registry import RegistryTaskProvider, StaticSnapshots, TaskRegistry, default_task_registry
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore
from rlworld.trajectories.records import EpisodeKey, EpisodeRecord

SHAPE = EnvShape(pool="windows", memory_mib=6144, vcpus=2, cpu_millicores=2000)
RUBRIC = """---
id: summary_quality
version: 3
guidance: Judge the summary email.
---
- tone: The email is professional.
"""


class LeaseRecordingFleet(FakeFleet):
    """The fake fleet that also keeps every lease request."""

    def __init__(self):
        super().__init__()
        self.requests = []

    def lease_group(self, request):
        self.requests.append(request)
        return super().lease_group(request)


def tenant_of(run_id):
    return f"tenant_of_{run_id}"


def eval_document(example_payload, licenses=()):
    """The example eval spec; its image's licenses only when a test brings a broker."""
    payload = copy.deepcopy(example_payload)
    payload["image"]["licenses"] = list(licenses)
    return rollout_document(payload, run_updates={"type": "eval", "group_size": 2})


def broker():
    return EntitlementBroker(seat_pools=[SeatPoolConfig("office_ltsc", 8, issues_token=True),
                                         SeatPoolConfig("windows_server_dc", 2, scope="per_host")],
                             identity_pools=[IdentityPoolConfig("finance_manager", ("fm1@corp.example",))],
                             session_minter=SignedSessionMinter(b"signing key", "crm.example", time.time),
                             secret_issuer=EphemeralSecretIssuer(time.time))


def platform_workers(document, fleet, gateway, **options):
    return PlatformWorkers(fleet, gateway, TrajectoryStore(InMemoryObjectStore()), TaskRegistry(verifier_registry()),
                           StaticSnapshots({}), {document.image.pool.vmm: SHAPE}, tenant_of, b"judge nonce secret",
                           **options)


def test_platform_workers_play_with_the_runs_policy_on_its_images_pool(example_payload):
    document = eval_document(example_payload)
    fleet, gateway = LeaseRecordingFleet(), ScriptedGateway({0: SOLVING_REPLIES})
    runner = platform_workers(document, fleet, gateway).group_runner("run_1", document)
    result = runner.run(task_instance(document), 2, 0, "run_1_g00000", "eval")
    assert [episode.reward.termination for episode in result.episodes] == ["success", "failure"]
    request = fleet.requests[0]
    assert (request.run_id, request.tenant, request.pool, request.memory_mib, request.vcpus, request.priority) == (
        "run_1", "tenant_of_run_1", "windows", 6144, 2, "eval")
    assert request.cost_center == document.experiment.name and request.endpoint == document.run.policy.endpoint
    assert request.world_time == "2026-03-14T09:00:00+00:00"
    sent = gateway.requests[0]
    assert (sent.endpoint, sent.model, sent.run_id, sent.purpose) == (document.run.policy.endpoint,
                                                                     document.run.policy.model, "run_1", "policy")
    assert runner.episode_runner.user_simulator is None and runner.episode_runner.finisher.judge is None
    assert runner.episode_runner.npc_simulator is None


def test_licensed_images_hold_seats_from_the_broker_while_they_play(example_payload):
    document = eval_document(example_payload, licenses=("windows_server_dc", "office_ltsc"))
    seats = broker()
    workers = platform_workers(document, FakeFleet(), ScriptedGateway({0: SOLVING_REPLIES}), entitlements=seats)
    runner = workers.group_runner("run_1", document)
    assert runner.settings.licenses == ("windows_server_dc", "office_ltsc") and runner.entitlements is seats
    result = runner.run(task_instance(document), 2, 0, "run_1_g00000", "eval")
    assert [episode.reward.termination for episode in result.episodes] == ["success", "failure"]
    assert seats.leases == {} and seats.availability()["office_ltsc"] == 8
    unbrokered = platform_workers(document, FakeFleet(), ScriptedGateway()).group_runner("run_2", document)
    assert "no broker" in unbrokered.run(task_instance(document), 1, 0, "g", "eval").episodes[0].reward.error_detail


def test_a_rubric_task_gets_the_runs_judge_and_a_simulated_user_when_configured(example_payload, tmp_path):
    (tmp_path / "rubrics").mkdir()
    (tmp_path / "rubrics" / "summary_email_quality.md").write_text(RUBRIC)
    document = parse_spec_document(copy.deepcopy(example_payload))
    library = RubricLibrary(tmp_path / "rubrics")
    workers = platform_workers(document, FakeFleet(), ScriptedGateway(), rubric_library=library,
                               user_simulator=SimulatorEndpoint("frontier", "persona_model_2026_06"))
    judge = workers.judge("run_9", document)
    assert isinstance(judge, RubricJudge) and judge.judge_model == document.run.judge.model
    assert (judge.client.endpoint, judge.client.run_id) == (document.run.judge.endpoint, "run_9")
    simulator = workers.group_runner("run_9", document).episode_runner.user_simulator
    assert isinstance(simulator, GatewayUserSimulator)
    assert (simulator.endpoint, simulator.model, simulator.run_id) == ("frontier", "persona_model_2026_06", "run_9")
    colleagues = workers.group_runner("run_9", document).episode_runner.npc_simulator
    assert isinstance(colleagues, GatewayNpcSimulator)
    assert (colleagues.endpoint, colleagues.model, colleagues.run_id) == ("frontier", "persona_model_2026_06", "run_9")
    no_user = document.model_copy(update={"task": document.task.model_copy(update={"user_simulator": None})})
    runner = workers.group_runner("run_9", no_user).episode_runner
    assert runner.user_simulator is None and isinstance(runner.npc_simulator, GatewayNpcSimulator)
    unjudged = document.model_copy(update={"run": document.run.model_copy(update={"judge": None})})
    assert workers.judge("run_9", unjudged) is None
    assert platform_workers(document, FakeFleet(), ScriptedGateway()).judge("run_9", document) is None


def test_a_worker_draws_its_tasks_from_the_registry(example_payload):
    payload = copy.deepcopy(example_payload)
    payload["experiment"]["sweep"] = {}
    payload["task"]["difficulty"]["apps"] = 2
    payload["task"].pop("judge_rubric")
    payload["world"]["size"] = {"employees": 6, "customers": 6, "invoices_per_customer": 3, "emails_per_person": 2}
    payload["world"]["materialize"] = ["mail", "files"]
    document = parse_spec_document(payload)
    snapshots = StaticSnapshots({spec_digest(document.image): SnapshotSource("sha256:s", "", "", "")})
    meter = UsageMeter()
    screen = NothingBlocked()
    workers = PlatformWorkers(FakeFleet(), ScriptedGateway(), TrajectoryStore(InMemoryObjectStore()),
                              default_task_registry(), snapshots, {document.image.pool.vmm: SHAPE}, tenant_of,
                              b"judge nonce secret", meter=meter, control="the experiment service", screen=screen)
    worker = workers("run_3", document)
    assert isinstance(worker.task_provider, RegistryTaskProvider) and worker.control == "the experiment service"
    assert worker.meter is meter and worker.run_id == "run_3"
    assert worker.task_provider.instance(worker.task_provider.candidates()[0]).snapshot.digest == "sha256:s"
    assert document.task.split == "train" and worker.task_provider.screen is screen
    assert screen.screened == len(worker.task_provider.candidates())


class NothingBlocked:
    """A training screen that counts what it saw and blocks nothing."""

    def __init__(self):
        self.screened = 0

    def blocked(self, task):
        self.screened += 1
        return None


def test_the_strong_agent_plays_each_try_in_a_fresh_env(example_payload):
    document = eval_document(example_payload)
    fleet, gateway = LeaseRecordingFleet(), ScriptedGateway({0: SOLVING_REPLIES})
    workers = platform_workers(document, fleet, gateway)
    agent = document.run.policy.model_copy(update={"endpoint": "frontier", "model": "strong_agent_2026_06"})
    probe = PlatformSolvabilityProbe(workers, agent)
    attempts = probe.attempts(document, task_instance(document), 3)
    assert [attempt.name for attempt in attempts] == ["strong_agent_0", "strong_agent_1", "strong_agent_2"]
    assert attempts[0].final_state == SOLVED_STATE and attempts[1].final_state == INITIAL_STATE
    assert attempts[0].outcome.final_action == "done" and attempts[0].outcome.steps_taken == 2
    assert {(request.endpoint, request.model) for request in gateway.requests} == {("frontier",
                                                                                   "strong_agent_2026_06")}
    assert {request.run_id for request in gateway.requests} == {"solvability_000001"}
    assert (fleet.requests[0].group_size, fleet.requests[0].priority) == (1, "eval")
    assert probe.attempts(document, task_instance(document), 1)[0].name == "strong_agent_0"
    assert gateway.requests[-1].run_id == "solvability_000002"


def reward(termination, detail=None):
    provenance = Provenance(state_hash="sha256:s", ground_truth_digest="sha256:g", reward_policy=RewardPolicy())
    return RewardRecord(total=0.0, components={}, termination=termination, counts_for_training=True, checks=(),
                        provenance=provenance, error_detail=detail)


@pytest.mark.parametrize(("termination", "detail", "expected"), [
    ("success", None, {"final_action": "done"}),
    ("failure", None, {"final_action": "done"}),
    ("max_steps", None, {"final_action": None}),
    ("agent_error", "unparseable reply", {"agent_error": "unparseable reply"}),
    ("env_error", "vm crashed", {"env_error": "vm crashed"}),
])
def test_played_episodes_read_back_as_gate_outcomes(termination, detail, expected):
    episode = EpisodeRecord(episode_id="e", key=EpisodeKey(image_snapshot_digest="sha256:i", world_digest="sha256:w",
                                                           task_digest="sha256:t", run_id="r", seed=1,
                                                           policy_version=0),
                            task_id="t", task_template="tasks.x", difficulty_key="d", instruction="i",
                            run_type="eval", reward=reward(termination, detail), started_at="2026-10-03T12:00:00Z")
    outcome = episode_outcome(episode, 7)
    assert outcome.steps_taken == 7
    for field, value in expected.items():
        assert getattr(outcome, field) == value
    unfinished = episode.model_copy(update={"reward": None})
    assert episode_outcome(unfinished, 0).env_error == "the episode never finished"
