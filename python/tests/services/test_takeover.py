import json

import httpx
import pytest
from support.experiment_fakes import FlaskBridge, build_experiment_service, eval_payload
from support.trajectory_fakes import episode, reward

from rlworld.sdk import TakenOverError
from rlworld.services.admin import PlatformAuditLog
from rlworld.services.experimentd_http import create_app
from rlworld.services.experiments import UnknownRecordError
from rlworld.services.takeover import TakeoverError, TakeoverService, demonstration_key
from rlworld.trajectories import InMemoryObjectStore, TrajectoryStore

SESSION = {"session_id": "a" * 64, "relay_path": f"/v1/takeover/{'a' * 64}", "expires_at": "2026-10-03T12:02:00Z"}
RECORD = {
    "events": [
        {"at_ms": 100, "kind": "pointer", "x": 40, "y": 50, "buttons": 1, "keysym": 0, "down": False},
        {"at_ms": 150, "kind": "pointer", "x": 40, "y": 50, "buttons": 0, "keysym": 0, "down": False},
        {"at_ms": 300, "kind": "key", "x": 0, "y": 0, "buttons": 0, "keysym": 0x31, "down": True},
        {"at_ms": 305, "kind": "key", "x": 0, "y": 0, "buttons": 0, "keysym": 0x31, "down": False},
    ],
    "lost_reason": "",
    "started_at": "2026-10-03T12:00:00Z",
    "ended_at": "2026-10-03T12:03:00Z",
}


class FakeTakeoverFleet:
    def __init__(self, refuse=False):
        self.refuse = refuse
        self.calls = []

    def start_takeover(self, lease_id):
        self.calls.append(("start", lease_id))
        if self.refuse:
            raise TakenOverError(409, "taken_over", "an operator has taken over this env")
        return SESSION

    def end_takeover(self, lease_id):
        self.calls.append(("end", lease_id))
        return RECORD


def running_store():
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    trajectory_store.start_episode(episode("episode_live", run_id="run_1", lease_id="lease_0007"), {}, {})
    trajectory_store.start_episode(episode("episode_leaseless", run_id="run_1"), {}, {})
    writer = trajectory_store.start_episode(episode("episode_done", run_id="run_1", lease_id="lease_0001"), {}, {})
    writer.finish(reward(), {})
    return trajectory_store


def test_takeovers_start_on_live_episodes_only():
    fleet = FakeTakeoverFleet()
    takeovers = TakeoverService(fleet, running_store())
    session = takeovers.start("run_1", "episode_live")
    assert session.session_id == SESSION["session_id"] and session.episode_id == "episode_live"
    assert fleet.calls == [("start", "lease_0007")]
    with pytest.raises(TakeoverError, match="already finished"):
        takeovers.start("run_1", "episode_done")
    with pytest.raises(TakeoverError, match="no env lease"):
        takeovers.start("run_1", "episode_leaseless")
    with pytest.raises(UnknownRecordError):
        takeovers.start("run_1", "episode_missing")


def test_handing_back_stores_the_demonstration():
    trajectory_store = running_store()
    takeovers = TakeoverService(FakeTakeoverFleet(), trajectory_store)
    demonstration = takeovers.end("run_1", "episode_live", "operator@example.com")
    assert demonstration.complete and demonstration.recorded_by == "operator@example.com"
    assert demonstration.actions == [
        {"name": "click", "x": 40, "y": 50, "button": "left", "count": 1, "modifiers": []},
        {"name": "type", "text": "1"},
    ]
    assert trajectory_store.object_store.exists(demonstration_key("run_1", "episode_live"))
    assert takeovers.demonstration("run_1", "episode_live") == demonstration
    with pytest.raises(UnknownRecordError):
        takeovers.demonstration("run_1", "episode_done")


def test_takeover_routes_are_audited_without_the_session_credential(example_payload, tmp_path):
    service = build_experiment_service(eval_payload(example_payload), tmp_path)
    audit = PlatformAuditLog()
    app = create_app(service, audit_log_store=audit,
                     takeover_service=TakeoverService(FakeTakeoverFleet(), running_store()))
    client = httpx.Client(transport=httpx.MockTransport(FlaskBridge(app)), base_url="http://experimentd.internal")
    started = client.post("/v1/runs/run_1/episodes/episode_live/takeover")
    assert started.status_code == 201 and started.json()["relay_path"] == SESSION["relay_path"]
    ended = client.post("/v1/runs/run_1/episodes/episode_live/takeover/end")
    assert ended.status_code == 200 and len(ended.json()["actions"]) == 2
    refused = client.post("/v1/runs/run_1/episodes/episode_done/takeover")
    assert refused.status_code == 409 and refused.json()["code"] == "takeover_refused"
    records = audit.query()
    assert {record.action for record in records} == {"start_takeover", "end_takeover"}
    assert SESSION["session_id"] not in json.dumps([record.model_dump(mode="json") for record in records])
    busy_app = create_app(service, takeover_service=TakeoverService(FakeTakeoverFleet(refuse=True), running_store()))
    busy = httpx.Client(transport=httpx.MockTransport(FlaskBridge(busy_app)), base_url="http://experimentd.internal")
    assert busy.post("/v1/runs/run_1/episodes/episode_live/takeover").json()["code"] == "takeover_refused"
    unconfigured = httpx.Client(transport=httpx.MockTransport(FlaskBridge(create_app(service))),
                                base_url="http://experimentd.internal")
    assert unconfigured.post("/v1/runs/run_1/episodes/episode_live/takeover").status_code == 503
