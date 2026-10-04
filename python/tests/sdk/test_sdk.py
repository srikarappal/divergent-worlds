import io
import json

import httpx
import pytest

from rlworld.actions.models import ClickAction, DoneAction, ExecAction, GotoAction, Observation
from rlworld.sdk import (
    AdmissionRejectedError,
    ComputerUseEnv,
    EnvError,
    EnvToolServer,
    FleetClient,
    InputFrozenError,
    LeaseGroupRequest,
    LeaseInfo,
    SnapshotNotFoundError,
    SnapshotSource,
    UnknownLeaseError,
)

OBSERVATION = {"step_index": 1, "screenshot": {"uri": "s3://frames/1.webp", "width": 1280, "height": 800,
                                              "sha256": "aa"}}
LEASE = {"lease_id": "lease_00000001", "env_id": "env_00000001", "host_id": "host_a",
         "expires_at": "2026-10-03T12:01:00Z"}


class FakeFleet:
    """Plays fleetd for the client: records requests and answers by route."""

    def __init__(self, errors=None):
        self.requests = []
        self.errors = errors or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path in self.errors:
            status, code = self.errors[request.url.path]
            return httpx.Response(status, json={"code": code, "message": "nope"})
        if request.method == "POST" and request.url.path == "/v1/leases":
            return httpx.Response(201, json={"leases": [LEASE, {**LEASE, "lease_id": "lease_00000002"}]})
        if request.url.path.endswith(("/step", "/reset")):
            return httpx.Response(200, json={"observation": OBSERVATION, "env_seconds": 0.8,
                                             "settle_seconds": 0.4, "settle_capped": False})
        if request.url.path.endswith("/finish"):
            return httpx.Response(200, json={"frozen": True, "final_state": {"mail": {"sent": []}}})
        if request.method == "DELETE" or request.url.path.endswith("/inject"):
            return httpx.Response(204)
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json=LEASE)
        return httpx.Response(200, json={"leases": [LEASE]})


def fleet_client(fake_fleet):
    return FleetClient("http://fleetd.internal", httpx.Client(transport=httpx.MockTransport(fake_fleet)))


def test_client_round_trips_every_call():
    fake_fleet = FakeFleet()
    client = fleet_client(fake_fleet)
    snapshot = SnapshotSource("sha256:snap", "/s/base.state", "/s/base.memory", "/s/base.ext4")
    leases = client.lease_group(LeaseGroupRequest(run_id="run_1", tenant="lab", pool="firecracker",
                                                  priority="training", endpoint="policy", snapshot=snapshot,
                                                  memory_mib=2048, vcpus=2, group_size=2))
    assert [lease.lease_id for lease in leases] == ["lease_00000001", "lease_00000002"]
    result = client.step("lease_00000001", ClickAction(x=5, y=6))
    assert isinstance(result.observation, Observation)
    assert (result.env_seconds, result.settle_capped) == (0.8, False)
    assert client.finish("lease_00000001", ["mail"]) == {"mail": {"sent": []}}
    client.inject("lease_00000001", {"browser_cookies": {}})
    client.close("lease_00000001")
    sent = {(method, path): body for method, path, body in fake_fleet.requests}
    assert sent[("POST", "/v1/leases")]["snapshot"]["digest"] == "sha256:snap"
    assert sent[("POST", "/v1/leases/lease_00000001/step")]["action"]["name"] == "click"
    assert ("DELETE", "/v1/leases/lease_00000001") in sent
    assert client.leases("run_1")[0].env_id == "env_00000001"
    assert client.heartbeat("lease_00000001").host_id == "host_a"


@pytest.mark.parametrize(
    ("status", "code", "error_class"),
    [(429, "fleet_full", AdmissionRejectedError), (409, "input_frozen", InputFrozenError),
     (404, "unknown_lease", UnknownLeaseError), (500, "env_error", EnvError)],
)
def test_errors_map_to_exception_types(status, code, error_class):
    client = fleet_client(FakeFleet(errors={"/v1/leases/lease_00000001/step": (status, code)}))
    with pytest.raises(error_class) as raised:
        client.step("lease_00000001", ClickAction(x=1, y=1))
    assert raised.value.code == code


def test_non_canonical_observations_stay_dicts():
    fake_fleet = FakeFleet()
    client = fleet_client(fake_fleet)
    assert isinstance(client.step_result({"observation": {"env": "x"}}).observation, dict)


class FakeEnvApi:
    def __init__(self):
        self.steps = []
        self.finished_with = None
        self.closed = False

    def reset(self, lease_id):
        return fleet_client(FakeFleet()).step_result({"observation": OBSERVATION})

    def step(self, lease_id, action):
        self.steps.append(action)
        return fleet_client(FakeFleet()).step_result({"observation": OBSERVATION, "env_seconds": 1.0})

    def finish(self, lease_id, apps):
        self.finished_with = apps
        return {"files": {"report.csv": {}}}

    def close(self, lease_id):
        self.closed = True


def reward_when_report_exists(final_state, outcome):
    return 1.0 if "report.csv" in final_state["files"] else 0.0


def make_env(api, max_steps=3, modalities=("computer", "control")):
    return ComputerUseEnv(api, LeaseInfo(**LEASE), modalities, max_steps, ["files"],
                          reward_function=reward_when_report_exists)


def test_env_steps_finishes_and_scores():
    api = FakeEnvApi()
    env = make_env(api)
    observation, info = env.reset()
    assert isinstance(observation, Observation)
    assert env.step(ClickAction(x=1, y=2))[1:4] == (0.0, False, False)
    _, reward, terminated, truncated, info = env.step(DoneAction())
    assert (reward, terminated, truncated) == (1.0, True, False)
    assert info["outcome"].final_action == "done"
    assert api.finished_with == ["files"]
    with pytest.raises(RuntimeError):
        env.step(ClickAction(x=1, y=2))
    env.close()
    assert api.closed


def test_env_truncates_at_the_step_budget():
    env = make_env(FakeEnvApi(), max_steps=2)
    env.reset()
    env.step(ClickAction(x=1, y=2))
    _, reward, terminated, truncated, info = env.step(ClickAction(x=1, y=2))
    assert (terminated, truncated, reward) == (False, True, 1.0)
    assert info["outcome"].final_action is None


def test_env_rejects_disallowed_modalities():
    env = make_env(FakeEnvApi())
    env.reset()
    with pytest.raises(ValueError):
        env.step(ExecAction(command="ls"))


def test_mcp_server_handshake_lists_and_calls_tools():
    api = FakeEnvApi()
    env = make_env(api, modalities=("computer", "browser", "control"))
    env.reset()
    server = EnvToolServer(env, image_fetcher=lambda uri: b"webp bytes")
    initialized = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": "2025-03-26"}})
    assert initialized["result"]["protocolVersion"] == "2025-03-26"
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tool_names = [tool["name"] for tool in server.handle_message({"jsonrpc": "2.0", "id": 2,
                                                                  "method": "tools/list"})["result"]["tools"]]
    assert tool_names[0] == "observe" and "goto" in tool_names and "exec" not in tool_names
    called = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "click", "arguments": {"x": 10, "y": 20}}})["result"]
    assert not called["isError"]
    assert called["content"][1] == {"type": "image", "data": "d2VicCBieXRlcw==", "mimeType": "image/webp"}
    assert api.steps == [ClickAction(x=10, y=20)]
    server.call_tool("goto", {"url": "https://crm.example.com"})
    assert api.steps[-1] == GotoAction(url="https://crm.example.com")


def test_mcp_tool_errors_and_episode_end():
    env = make_env(FakeEnvApi())
    env.reset()
    server = EnvToolServer(env)
    assert server.call_tool("exec", {"command": "ls"})["isError"]
    assert server.call_tool("click", {"x": -5, "y": 1})["isError"]
    finished = server.call_tool("done", {})
    assert json.loads(finished["content"][-1]["text"])["episode_finished"] is True
    assert server.handle_message({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})["error"]["code"] == -32601


def test_mcp_serves_newline_delimited_json_over_streams():
    env = make_env(FakeEnvApi())
    env.reset()
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "{broken json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "observe"}}),
    ]) + "\n"
    output_stream = io.StringIO()
    EnvToolServer(env).serve(io.StringIO(requests), output_stream)
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert [response.get("id") for response in responses] == [1, None, 2]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert responses[1]["error"]["code"] == -32700
    assert "screenshot: s3://frames/1.webp" in responses[2]["result"]["content"][1]["text"]


class FakeSnapshotStore:
    """Plays fleetd's snapshot routes for the client."""

    def __init__(self):
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.raw_path.decode(), body))
        path = request.url.path
        if path == "/v1/snapshots/current":
            if request.url.params["pool"] == "nowhere":
                return httpx.Response(404, json={"code": "unknown_snapshot", "message": "no current snapshot"})
            return httpx.Response(200, json={"image": "noble_calc_v1", "snapshot": "sha256:s"})
        if path == "/v1/snapshots":
            return httpx.Response(200, json={"current": [{"snapshot": "sha256:s"}]})
        if path == "/v1/snapshots/build":
            return httpx.Response(201, json={"digest": "sha256:s", "image": "noble_calc_v1"})
        if path == "/v1/snapshots/collect":
            return httpx.Response(200, json={"removed": [], "removed_chunks": 0, "freed_bytes": 0})
        if path.endswith("/prefetch"):
            return httpx.Response(202, json={"hosts": ["host_a"]})
        if "/pins/" in path:
            return httpx.Response(204) if request.method == "DELETE" else httpx.Response(200, json={"holder": "x"})
        return httpx.Response(200, json={"manifest": {"digest": "sha256:s"}, "pins": []})


def test_the_client_reaches_every_snapshot_route():
    store = FakeSnapshotStore()
    client = fleet_client(store)
    assert client.build_snapshot("/builds/noble", 2048, 2)["digest"] == "sha256:s"
    assert client.current_snapshots() == [{"snapshot": "sha256:s"}]
    assert client.current_snapshot("sha256:image", "linux")["snapshot"] == "sha256:s"
    assert client.snapshot("sha256:s")["manifest"]["digest"] == "sha256:s"
    assert client.prefetch_snapshot("sha256:s", "linux", 64, 2048, 2000) == ["host_a"]
    client.pin_snapshot("sha256:s", "eval_report:experiment/1")
    client.unpin_snapshot("sha256:s", "eval_report:experiment/1")
    assert client.collect_snapshots()["removed"] == []
    sent = [(method, path) for method, path, _ in store.requests]
    assert sent == [
        ("POST", "/v1/snapshots/build"), ("GET", "/v1/snapshots"),
        ("GET", "/v1/snapshots/current?image_digest=sha256%3Aimage&pool=linux"), ("GET", "/v1/snapshots/sha256:s"),
        ("POST", "/v1/snapshots/sha256:s/prefetch"), ("PUT", "/v1/snapshots/sha256:s/pins/eval_report:experiment%2F1"),
        ("DELETE", "/v1/snapshots/sha256:s/pins/eval_report:experiment%2F1"), ("POST", "/v1/snapshots/collect")]
    bodies = {path: body for _, path, body in store.requests}
    assert bodies["/v1/snapshots/build"] == {"build_dir": "/builds/noble", "memory_mib": 2048, "vcpus": 2}
    assert bodies["/v1/snapshots/sha256:s/prefetch"] == {"pool": "linux", "envs": 64, "memory_mib": 2048,
                                                         "cpu_millicores": 2000, "gpus": 0}
    with pytest.raises(SnapshotNotFoundError) as missing:
        client.current_snapshot("sha256:image", "nowhere")
    assert missing.value.code == "unknown_snapshot" and missing.value.status_code == 404
