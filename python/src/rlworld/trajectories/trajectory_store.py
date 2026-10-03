"""Write and read episodes: episode.json per episode, one object per step, states as blobs.

The episode file is written at the start too, so the console can show running
episodes; finish rewrites it with the reward and the final state.
"""

import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from rlworld.rewards.records import RewardRecord
from rlworld.trajectories.object_store import ObjectStore, get_blob, put_blob
from rlworld.trajectories.records import EpisodeRecord, StepRecord

IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_]+")


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def check_identifier(identifier: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"identifier {identifier!r} may only use letters, digits and underscores")
    return identifier


def episode_prefix(run_id: str, episode_id: str) -> str:
    return f"runs/{check_identifier(run_id)}/episodes/{check_identifier(episode_id)}"


def step_number(step_key: str) -> int:
    """steps/000012.json holds step 12."""
    return int(step_key.rsplit("/", 1)[1].removesuffix(".json"))


class TrajectoryStore:
    def __init__(self, object_store: ObjectStore) -> None:
        self.object_store = object_store

    def put_json_blob(self, payload: Any) -> str:
        return put_blob(self.object_store, json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))

    def load_json_blob(self, digest: str) -> Any:
        return json.loads(get_blob(self.object_store, digest))

    def save_episode(self, episode: EpisodeRecord) -> None:
        episode_json = episode.model_dump_json(exclude={"steps"})
        self.object_store.put_bytes(f"{episode_prefix(episode.key.run_id, episode.episode_id)}/episode.json",
                                    episode_json.encode("utf-8"))

    def start_episode(
        self, episode: EpisodeRecord, initial_state: dict[str, Any], ground_truth: dict[str, Any]
    ) -> "TrajectoryWriter":
        started_episode = episode.model_copy(
            update={
                "initial_state_ref": self.put_json_blob(initial_state),
                "ground_truth_ref": self.put_json_blob(ground_truth),
            }
        )
        self.save_episode(started_episode)
        return TrajectoryWriter(self, started_episode)

    def step_keys(self, run_id: str, episode_id: str, first_step: int = 0) -> list[str]:
        step_keys = self.object_store.list_keys(f"{episode_prefix(run_id, episode_id)}/steps/")
        return [step_key for step_key in step_keys if step_number(step_key) >= first_step]

    def load_steps(self, run_id: str, episode_id: str, first_step: int = 0) -> tuple[StepRecord, ...]:
        """Steps from first_step on, so a live view fetches only what is new."""
        return tuple(StepRecord.model_validate_json(self.object_store.get_bytes(step_key))
                     for step_key in self.step_keys(run_id, episode_id, first_step))

    def last_step(self, run_id: str, episode_id: str) -> StepRecord | None:
        step_keys = self.step_keys(run_id, episode_id)
        return StepRecord.model_validate_json(self.object_store.get_bytes(step_keys[-1])) if step_keys else None

    def load_episode(self, run_id: str, episode_id: str, with_steps: bool = True) -> EpisodeRecord:
        episode_bytes = self.object_store.get_bytes(f"{episode_prefix(run_id, episode_id)}/episode.json")
        episode = EpisodeRecord.model_validate_json(episode_bytes)
        if with_steps:
            episode = episode.model_copy(update={"steps": self.load_steps(run_id, episode_id)})
        return episode

    def list_episode_ids(self, run_id: str) -> list[str]:
        episode_keys = self.object_store.list_keys(f"runs/{check_identifier(run_id)}/episodes/")
        return sorted({episode_key.split("/")[3] for episode_key in episode_keys
                       if episode_key.endswith("/episode.json")})

    def iter_episodes(self, run_id: str, with_steps: bool = False) -> Iterator[EpisodeRecord]:
        for episode_id in self.list_episode_ids(run_id):
            yield self.load_episode(run_id, episode_id, with_steps)

    def load_state(self, state_ref: str) -> dict[str, Any]:
        return self.load_json_blob(state_ref)


class TrajectoryWriter:
    def __init__(self, trajectory_store: TrajectoryStore, episode: EpisodeRecord) -> None:
        self.trajectory_store = trajectory_store
        self.episode = episode
        self.next_step_index = 0

    def put_prompt(self, messages: Any) -> str:
        """The exact request messages for a step, stored once and referenced by digest."""
        return self.trajectory_store.put_json_blob(messages)

    def write_step(self, step: StepRecord) -> None:
        if step.step_index != self.next_step_index:
            raise ValueError(f"expected step {self.next_step_index}, got {step.step_index}")
        prefix = episode_prefix(self.episode.key.run_id, self.episode.episode_id)
        step_key = f"{prefix}/steps/{step.step_index:06d}.json"
        self.trajectory_store.object_store.put_bytes(step_key, step.model_dump_json().encode("utf-8"))
        self.next_step_index += 1

    def finish(
        self,
        reward: RewardRecord,
        final_state: dict[str, Any],
        final_snapshot_ref: str | None = None,
        human_intervened: bool = False,
        retention: str = "full",
    ) -> EpisodeRecord:
        finished_episode = self.episode.model_copy(
            update={
                "reward": reward,
                "final_state_ref": self.trajectory_store.put_json_blob(final_state),
                "final_snapshot_ref": final_snapshot_ref,
                "human_intervened": human_intervened or self.episode.human_intervened,
                "retention": retention,
                "finished_at": utc_timestamp(),
            }
        )
        self.trajectory_store.save_episode(finished_episode)
        self.episode = finished_episode
        return finished_episode
