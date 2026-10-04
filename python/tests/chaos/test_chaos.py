"""Chaos tests (DESIGN.md section 6.15): a host, a sidecar and the gateway die in the middle of an eval run.

Each run plays through experimentd against a fleet or a model endpoint that
fails partway. Every failure must end as an env error, which is excluded from
training and counted on /metrics; groups retry env errors on fresh envs; the
run still completes with every task scored.
"""

import threading

from support.experimentd_fakes import SECRETS, RecordingLaunches, ScriptedTransport, laptop_config, small_reference_spec
from support.rollout_fakes import FakeFleet

from rlworld.gateway.errors import UpstreamError
from rlworld.sdk.client import FleetError
from rlworld.services.experimentd_main import build_experimentd

TASKS = 2


class HostDiesMidRun(FakeFleet):
    """The host serving the first group dies at its first step; leases opened after that land on a healthy host."""

    def __init__(self):
        super().__init__()
        self.dead_leases = set()
        self.died = threading.Event()

    def fail_if_dead(self, lease_id):
        if lease_id in self.dead_leases:
            raise FleetError(502, "host_down", f"the host of {lease_id} stopped answering")

    def step(self, lease_id, action):
        if not self.died.is_set():
            with self.lock:
                self.dead_leases.update(self.open_leases)
            self.died.set()
        self.fail_if_dead(lease_id)
        return super().step(lease_id, action)

    def finish(self, lease_id, apps):
        self.fail_if_dead(lease_id)
        return super().finish(lease_id, apps)

    def close(self, lease_id):
        self.record("close_attempt", lease_id)
        self.fail_if_dead(lease_id)
        super().close(lease_id)


class SidecarDiesMidRun(FakeFleet):
    """The extraction sidecar is down for the first finishes, so those episodes cannot be scored."""

    def __init__(self, failures):
        super().__init__()
        self.failures_left = failures

    def finish(self, lease_id, apps):
        with self.lock:
            failing = self.failures_left > 0
            self.failures_left -= failing
        if failing:
            self.record("finish", lease_id, tuple(apps))
            raise FleetError(502, "sidecar_down", "the mail sidecar stopped answering")
        return super().finish(lease_id, apps)


class GatewayDiesMidRun(ScriptedTransport):
    """The policy endpoint refuses connections for its first calls, then answers again."""

    def __init__(self, failures):
        super().__init__()
        self.failures_left = failures

    def send(self, endpoint, api_key, request):
        if request.purpose == "policy":
            with self.lock:
                failing = self.failures_left > 0
                self.failures_left -= failing
            if failing:
                raise UpstreamError("connection refused", retryable=False)
        return super().send(endpoint, api_key, request)


def play(tmp_path, fleet, transport):
    experimentd = build_experimentd(laptop_config(tmp_path), SECRETS, fleet=fleet, transport=transport,
                                    launch_runner=RecordingLaunches())
    client = experimentd.app.test_client()
    digest = client.post("/v1/specs", data=small_reference_spec(), content_type="application/yaml").get_json()[
        "digest"]
    run_id = client.post(f"/v1/specs/{digest}/launch", json={"budget_usd": 10.0}).get_json()["run_ids"][0]
    assert experimentd.executor.wait(run_id, timeout_seconds=120)
    assert client.get(f"/v1/runs/{run_id}").get_json()["state"] == "completed"
    episodes = client.get(f"/v1/runs/{run_id}/episodes", query_string={"status": "finished"}).get_json()["episodes"]
    return experimentd, client, episodes


def terminations(episodes):
    return sorted(episode["reward"]["termination"] for episode in episodes)


def env_errors_on_metrics(client):
    total = 0.0
    for line in client.get("/metrics").get_data(as_text=True).splitlines():
        if line.startswith("rlworld_episodes_total{") and 'termination="env_error"' in line:
            total += float(line.rpartition(" ")[2])
    return total


def scored_tasks(episodes):
    return {episode["task_id"] for episode in episodes if episode["reward"]["termination"] != "env_error"}


def test_a_host_dying_mid_run_turns_its_envs_into_env_errors_that_retry_elsewhere(tmp_path):
    fleet = HostDiesMidRun()
    experimentd, client, episodes = play(tmp_path, fleet, ScriptedTransport())
    assert fleet.died.is_set() and fleet.dead_leases
    assert terminations(episodes).count("env_error") == len(fleet.dead_leases)
    assert len(scored_tasks(episodes)) == TASKS
    assert env_errors_on_metrics(client) == len(fleet.dead_leases)
    attempted_closes = {call[1] for call in fleet.calls_named("close_attempt")}
    assert fleet.dead_leases <= attempted_closes and fleet.open_leases == fleet.dead_leases


def test_a_sidecar_dying_mid_run_leaves_unscorable_episodes_as_env_errors(tmp_path):
    fleet = SidecarDiesMidRun(failures=1)
    experimentd, client, episodes = play(tmp_path, fleet, ScriptedTransport())
    assert terminations(episodes).count("env_error") == 1
    failed = next(episode for episode in episodes if episode["reward"]["termination"] == "env_error")
    assert "sidecar_down" in failed["reward"]["error_detail"]
    assert len(scored_tasks(episodes)) == TASKS and env_errors_on_metrics(client) == 1
    assert fleet.open_leases == set()


def test_the_gateway_dying_mid_run_fails_its_policy_call_as_an_env_error_that_retries(tmp_path):
    fleet = FakeFleet()
    experimentd, client, episodes = play(tmp_path, fleet, GatewayDiesMidRun(failures=1))
    failed = [episode for episode in episodes if episode["reward"]["termination"] == "env_error"]
    assert len(failed) == 1 and "policy call failed" in failed[0]["reward"]["error_detail"]
    assert len(scored_tasks(episodes)) == TASKS and fleet.open_leases == set()
    metrics_text = client.get("/metrics").get_data(as_text=True)
    assert 'rlworld_gateway_calls_total{endpoint="local_policy",purpose="policy",outcome="error"} 1' in metrics_text


def test_a_gateway_down_for_the_whole_run_still_lets_the_run_finish(tmp_path):
    fleet = FakeFleet()
    experimentd, client, episodes = play(tmp_path, fleet, GatewayDiesMidRun(failures=1_000))
    attempts_per_task = 2
    assert terminations(episodes) == ["env_error"] * (TASKS * attempts_per_task)
    assert scored_tasks(episodes) == set() and fleet.open_leases == set()
    assert env_errors_on_metrics(client) == TASKS * attempts_per_task
