"""Operator takeover of a running episode (DESIGN.md section 6.14).

An operator takes over a live episode from the console: fleetd keeps its env
running for them, refuses the rollout worker's steps and relays noVNC to the
env's screen. The worker marks the episode human intervened, which keeps it out
of training and eval statistics, waits, then scores whatever state the operator
leaves. Handing back stores the operator's input next to the episode as a
demonstration in canonical actions, ready to serve as SFT data or an oracle.
"""

from typing import Any, Protocol

from pydantic import BaseModel

from rlworld.actions.demonstrations import InputEvent, demonstration_actions
from rlworld.actions.models import action_payload
from rlworld.services.experiments import UnknownRecordError
from rlworld.trajectories.trajectory_store import TrajectoryStore


class TakeoverError(ValueError):
    """The episode cannot change hands: it has ended or has no env lease."""


class TakeoverFleet(Protocol):
    def start_takeover(self, lease_id: str) -> dict[str, Any]: ...

    def end_takeover(self, lease_id: str) -> dict[str, Any]: ...


class TakeoverSession(BaseModel):
    """relay_path is a fleetd path; the session id in it is the only credential the relay takes, once."""

    run_id: str
    episode_id: str
    session_id: str
    relay_path: str
    expires_at: str


class Demonstration(BaseModel):
    run_id: str
    episode_id: str
    recorded_by: str
    started_at: str
    ended_at: str
    complete: bool
    lost_reason: str = ""
    pause_error: str = ""
    events: list[InputEvent]
    actions: list[dict[str, Any]]


def demonstration_key(run_id: str, episode_id: str) -> str:
    return f"demonstrations/{run_id}/{episode_id}.json"


class TakeoverService:
    def __init__(self, fleet: TakeoverFleet, trajectory_store: TrajectoryStore) -> None:
        self.fleet = fleet
        self.trajectory_store = trajectory_store

    def live_lease(self, run_id: str, episode_id: str) -> str:
        try:
            episode = self.trajectory_store.load_episode(run_id, episode_id, with_steps=False)
        except (KeyError, ValueError) as error:
            raise UnknownRecordError(f"{run_id}/{episode_id}") from error
        if episode.finished_at is not None:
            raise TakeoverError(f"episode {episode_id} has already finished")
        if episode.lease_id is None:
            raise TakeoverError(f"episode {episode_id} has no env lease to take over")
        return episode.lease_id

    def start(self, run_id: str, episode_id: str) -> TakeoverSession:
        started = self.fleet.start_takeover(self.live_lease(run_id, episode_id))
        return TakeoverSession(run_id=run_id, episode_id=episode_id, **started)

    def end(self, run_id: str, episode_id: str, actor: str) -> Demonstration:
        record = self.fleet.end_takeover(self.live_lease(run_id, episode_id))
        events = [InputEvent.model_validate(event) for event in record.get("events") or []]
        lost_reason = record.get("lost_reason", "")
        demonstration = Demonstration(
            run_id=run_id, episode_id=episode_id, recorded_by=actor, started_at=record["started_at"],
            ended_at=record["ended_at"], complete=not lost_reason, lost_reason=lost_reason,
            pause_error=record.get("pause_error", ""), events=events,
            actions=[action_payload(action) for action in demonstration_actions(events)])
        self.trajectory_store.object_store.put_bytes(demonstration_key(run_id, episode_id),
                                                     demonstration.model_dump_json().encode("utf-8"))
        return demonstration

    def demonstration(self, run_id: str, episode_id: str) -> Demonstration:
        key = demonstration_key(run_id, episode_id)
        if not self.trajectory_store.object_store.exists(key):
            raise UnknownRecordError(f"no demonstration for {run_id}/{episode_id}")
        return Demonstration.model_validate_json(self.trajectory_store.object_store.get_bytes(key))
