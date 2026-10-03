import pytest
from support.gateway_fakes import FakeClock
from support.rollout_fakes import (
    SETUP_PAYLOAD,
    SNAPSHOT,
    FakeFleet,
    ScriptedGateway,
    build_group_runner,
    rollout_document,
    task_instance,
)

from rlworld.entitlements import (
    EntitlementBroker,
    EphemeralSecretIssuer,
    IdentityPoolConfig,
    SeatPoolConfig,
    SignedSessionMinter,
)
from rlworld.rollout import RunStoppedError
from rlworld.sdk import EnvError
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore

ACCOUNTS = tuple(f"finance_manager_{number:02d}" for number in range(6))


class RecordingSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


@pytest.fixture
def document(example_payload):
    return rollout_document(example_payload)


def run_group(document, fleet, group_size=4, scripts=None, **options):
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    group_runner = build_group_runner(document, fleet, ScriptedGateway(scripts), trajectory_store, **options)
    group = group_runner.run(task_instance(document), group_size, policy_version=2, group_id="b00000_g00000",
                             priority="training")
    return group, trajectory_store


def test_members_are_forks_of_one_start_state(document):
    fleet = FakeFleet()
    group, _ = run_group(document, fleet)
    assert fleet.calls_named("lease_group") == [("lease_group", 1, "training", SNAPSHOT.digest)]
    assert fleet.calls_named("inject") == [("inject", "lease_0001", SETUP_PAYLOAD)]
    assert fleet.calls_named("fork") == [("fork", "lease_0001", 3)]
    assert [episode.episode_id for episode in group.episodes] == [
        f"b00000_g00000_s{index:03d}_a0" for index in range(4)]
    assert [episode.reward.termination for episode in group.episodes] == ["success", "failure", "failure", "failure"]
    assert {episode.group_id for episode in group.episodes} == {"b00000_g00000"}
    assert group.replaced_episodes == () and fleet.open_leases == set()


def test_every_member_gets_its_own_sessions_and_seats(document):
    clock = FakeClock()
    broker = EntitlementBroker(
        seat_pools=[SeatPoolConfig("office_ltsc", 8, issues_token=True),
                    SeatPoolConfig("windows_server_dc", 2, scope="per_host")],
        identity_pools=[IdentityPoolConfig("finance_manager", ACCOUNTS)],
        session_minter=SignedSessionMinter(b"signing key", "crm.example", clock.time),
        secret_issuer=EphemeralSecretIssuer(clock.time),
        clock=clock.time,
    )
    fleet = FakeFleet()
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    group_runner = build_group_runner(document, fleet, ScriptedGateway(), trajectory_store,
                                      licenses=("windows_server_dc", "office_ltsc"), entitlements=broker)
    group = group_runner.run(task_instance(document, personas=("finance_manager",)), 4, 2, "b00000_g00000",
                             "training")
    member_injections = [call for call in fleet.calls_named("inject") if "browser_cookies" in call[2]]
    assert sorted(call[1] for call in member_injections) == [f"lease_{number:04d}" for number in range(1, 5)]
    cookie_sets = [tuple(sorted(call[2]["browser_cookies"].items())) for call in member_injections]
    assert len(set(cookie_sets)) == 4
    assert all(call[2]["license_tokens"]["office_ltsc"] for call in member_injections)
    assert group.episodes[0].reward.termination == "success"
    assert broker.leases == {} and broker.availability()["office_ltsc"] == 8


def test_a_missing_broker_is_an_env_error(document):
    fleet = FakeFleet()
    group, _ = run_group(document, fleet, licenses=("office_ltsc",), env_error_retries=0)
    assert {episode.reward.termination for episode in group.episodes} == {"env_error"}
    assert "no broker" in group.episodes[0].reward.error_detail
    assert fleet.open_leases == set()


def test_env_errors_are_replaced_by_forks_of_a_new_start_state(document):
    fleet = FakeFleet(crashing_leases={"lease_0003"})
    group, trajectory_store = run_group(document, fleet)
    assert [episode.reward.termination for episode in group.episodes] == ["success", "failure", "failure", "failure"]
    assert group.episodes[2].episode_id == "b00000_g00000_s002_a1"
    assert [episode.reward.termination for episode in group.replaced_episodes] == ["env_error"]
    assert fleet.calls_named("lease_group")[1][1] == 1 and len(fleet.calls_named("fork")) == 1
    assert len(trajectory_store.list_episode_ids("run_1")) == 5
    assert fleet.open_leases == set()


def test_full_fleets_are_waited_out(document):
    sleep = RecordingSleep()
    group, _ = run_group(document, FakeFleet(admission_rejections=2), sleep=sleep)
    assert sleep.calls == [2.0, 2.0]
    assert group.episodes[0].reward.termination == "success"


def test_setup_failures_become_env_errors_and_free_the_source(document):
    fleet = FakeFleet(fork_error=EnvError(500, "fork_failed", "checkpoint write failed"))
    group, _ = run_group(document, fleet, env_error_retries=0)
    assert {episode.reward.termination for episode in group.episodes} == {"env_error"}
    assert "fork failed" in group.episodes[0].reward.error_detail
    assert fleet.calls_named("close") == [("close", "lease_0001")] and fleet.open_leases == set()


def test_stopping_abandons_groups_still_waiting_for_capacity(document):
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    group_runner = build_group_runner(document, FakeFleet(admission_rejections=5), ScriptedGateway(),
                                      trajectory_store)
    group_runner.stop_event.set()
    with pytest.raises(RunStoppedError):
        group_runner.run(task_instance(document), 4, 0, "b00000_g00001", "training")
    assert trajectory_store.list_episode_ids("run_1") == []
