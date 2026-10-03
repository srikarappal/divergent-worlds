"""Read views over the trajectory store for the console (DESIGN.md sections 6.11 and 6.14).

Running episodes are visible as they play because every step is written when
it happens. The live wall samples running episodes and shows each one's latest
frame; a viewer that follows one episode asks only for the steps it has not seen.
Frames are content addressed blobs, served thumbnails first and at full
resolution on demand; thumbnails are made once and cached beside the frames.
"""

import io
from operator import attrgetter
from typing import Literal

from PIL import Image
from pydantic import BaseModel

from rlworld.services.experiments import UnknownRecordError
from rlworld.trajectories.object_store import get_blob
from rlworld.trajectories.records import EpisodeRecord
from rlworld.trajectories.trajectory_store import TrajectoryStore

FrameSize = Literal["full", "thumbnail"]
FRAME_SIGNATURES = ((b"RIFF", "image/webp"), (b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"))


class LiveTile(BaseModel):
    episode_id: str
    task_id: str
    task_template: str
    group_id: str | None
    sample_index: int
    step_index: int | None
    frame: str | None
    started_at: str


def frame_media_type(frame: bytes) -> str:
    for signature, media_type in FRAME_SIGNATURES:
        if frame.startswith(signature):
            return media_type
    return "application/octet-stream"


def thumbnail_of(frame: bytes, width: int, height: int) -> bytes:
    with Image.open(io.BytesIO(frame)) as image:
        image.thumbnail((width, height))
        output = io.BytesIO()
        image.convert("RGB").save(output, format="WEBP", quality=70)
    return output.getvalue()


class TrajectoryViews:
    def __init__(self, trajectory_store: TrajectoryStore, thumbnail_width: int = 320,
                 thumbnail_height: int = 200) -> None:
        self.trajectory_store = trajectory_store
        self.thumbnail_width = thumbnail_width
        self.thumbnail_height = thumbnail_height

    def episodes(self, run_id: str, status: Literal["running", "finished"] | None = None) -> list[EpisodeRecord]:
        try:
            episodes = list(self.trajectory_store.iter_episodes(run_id))
        except ValueError as error:
            raise UnknownRecordError(run_id) from error
        if status is None:
            return episodes
        return [episode for episode in episodes if (episode.finished_at is None) == (status == "running")]

    def episode(self, run_id: str, episode_id: str, first_step: int = 0) -> EpisodeRecord:
        try:
            episode = self.trajectory_store.load_episode(run_id, episode_id, with_steps=False)
        except (KeyError, ValueError) as error:
            raise UnknownRecordError(f"{run_id}/{episode_id}") from error
        return episode.model_copy(update={"steps": self.trajectory_store.load_steps(run_id, episode_id, first_step)})

    def live_wall(self, run_id: str, limit: int = 24) -> list[LiveTile]:
        """A stable sample of running episodes, each with its latest frame."""
        tiles = []
        for episode in sorted(self.episodes(run_id, "running"), key=attrgetter("episode_id"))[:limit]:
            last_step = self.trajectory_store.last_step(run_id, episode.episode_id)
            screenshot = last_step.observation.screenshot if last_step is not None else None
            tiles.append(LiveTile(
                episode_id=episode.episode_id,
                task_id=episode.task_id,
                task_template=episode.task_template,
                group_id=episode.group_id,
                sample_index=episode.sample_index,
                step_index=last_step.step_index if last_step is not None else None,
                frame=screenshot.sha256 if screenshot is not None else None,
                started_at=episode.started_at,
            ))
        return tiles

    def frame(self, frame_digest: str, size: FrameSize = "full") -> tuple[bytes, str]:
        hex_digest = frame_digest.removeprefix("sha256:")
        try:
            frame = get_blob(self.trajectory_store.object_store, f"sha256:{hex_digest}")
        except (KeyError, ValueError) as error:
            raise UnknownRecordError(f"frame {frame_digest}") from error
        if size == "full":
            return frame, frame_media_type(frame)
        thumbnail_key = f"thumbnails/{self.thumbnail_width}x{self.thumbnail_height}/{hex_digest}.webp"
        object_store = self.trajectory_store.object_store
        if not object_store.exists(thumbnail_key):
            object_store.put_bytes(thumbnail_key, thumbnail_of(frame, self.thumbnail_width, self.thumbnail_height))
        return object_store.get_bytes(thumbnail_key), "image/webp"
