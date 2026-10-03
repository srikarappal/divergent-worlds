"""Retention under each tenant's data policy (DESIGN.md sections 6.11 and 6.14).

Episodes past the tenant's trajectory window are deleted with the blobs only
they used. Episodes past the full frame window keep thumbnails only: each frame
gets its thumbnail first, then the full resolution blob goes unless a fresher
episode still shows it. Only blobs the deleted or aged episodes referenced are
touched, so a blob written for a step that is not recorded yet is never lost.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rlworld.services.admin import DataPolicyStore
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.trajectories.object_store import blob_key
from rlworld.trajectories.records import EpisodeRecord
from rlworld.trajectories.trajectory_store import episode_prefix

SECONDS_PER_DAY = 86_400


@dataclass
class EpisodeReferences:
    blobs: set[str] = field(default_factory=set)
    frames: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RetentionReport:
    deleted_episodes: tuple[str, ...]
    thumbnail_only_frames: int
    deleted_blobs: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def age_in_days(episode: EpisodeRecord, now: datetime) -> float | None:
    """Unfinished episodes have no age; they are never swept."""
    if episode.finished_at is None:
        return None
    return (now - datetime.fromisoformat(episode.finished_at)).total_seconds() / SECONDS_PER_DAY


def hex_digest(digest: str) -> str:
    return digest.removeprefix("sha256:")


class RetentionSweeper:
    def __init__(self, views: TrajectoryViews, policies: DataPolicyStore,
                 now: Callable[[], datetime] = utc_now) -> None:
        self.views = views
        self.policies = policies
        self.now = now

    def run_ids(self) -> list[str]:
        episode_keys = self.views.trajectory_store.object_store.list_keys("runs/")
        return sorted({key.split("/")[1] for key in episode_keys if key.endswith("/episode.json")})

    def references(self, run_id: str, episode: EpisodeRecord) -> EpisodeReferences:
        found = EpisodeReferences()
        for state_ref in (episode.initial_state_ref, episode.ground_truth_ref, episode.final_state_ref):
            if state_ref is not None:
                found.blobs.add(hex_digest(state_ref))
        for step in self.views.trajectory_store.load_steps(run_id, episode.episode_id):
            if step.model_turn.prompt_ref is not None:
                found.blobs.add(hex_digest(step.model_turn.prompt_ref))
            if step.observation.screenshot is not None:
                found.frames.add(hex_digest(step.observation.screenshot.sha256))
        return found

    def delete_episode(self, run_id: str, episode_id: str) -> None:
        object_store = self.views.trajectory_store.object_store
        for key in object_store.list_keys(f"{episode_prefix(run_id, episode_id)}/"):
            object_store.delete(key)

    def sweep(self) -> RetentionReport:
        now = self.now()
        kept = EpisodeReferences()
        aged_frames: set[str] = set()
        expired = EpisodeReferences()
        deleted_episodes = []
        for run_id in self.run_ids():
            policy = self.policies.run_policy(run_id)
            for episode in self.views.trajectory_store.iter_episodes(run_id):
                age = age_in_days(episode, now)
                references = self.references(run_id, episode)
                if age is not None and age > policy.trajectory_days:
                    expired.blobs |= references.blobs
                    expired.frames |= references.frames
                    self.delete_episode(run_id, episode.episode_id)
                    deleted_episodes.append(f"{run_id}/{episode.episode_id}")
                    continue
                kept.blobs |= references.blobs
                if age is not None and age > policy.full_frame_days:
                    aged_frames |= references.frames
                else:
                    kept.frames |= references.frames
        thumbnail_only = aged_frames - kept.frames
        for frame in thumbnail_only:
            self.drop_full_frame(frame)
        orphaned = (expired.blobs - kept.blobs) | (expired.frames - kept.frames - aged_frames)
        for digest in orphaned:
            self.delete_blob(digest)
        return RetentionReport(tuple(deleted_episodes), len(thumbnail_only), len(orphaned))

    def drop_full_frame(self, frame: str) -> None:
        object_store = self.views.trajectory_store.object_store
        if object_store.exists(blob_key(frame)):
            self.views.frame(frame, "thumbnail")
            object_store.delete(blob_key(frame))

    def delete_blob(self, digest: str) -> None:
        object_store = self.views.trajectory_store.object_store
        object_store.delete(blob_key(digest))
        for thumbnail_key in object_store.list_keys("thumbnails/"):
            if thumbnail_key.endswith(f"/{digest}.webp"):
                object_store.delete(thumbnail_key)
