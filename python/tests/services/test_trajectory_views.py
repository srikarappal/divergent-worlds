import io

import pytest
from PIL import Image
from support.experiment_fakes import build_experiment_service, eval_payload
from support.trajectory_fakes import episode, reward

from rlworld.actions.models import Observation
from rlworld.services.experimentd_http import create_app
from rlworld.services.experiments import UnknownRecordError
from rlworld.services.trajectory_views import TrajectoryViews
from rlworld.specs import dump_spec_document, parse_spec_document
from rlworld.trajectories import InMemoryObjectStore, StepRecord, TrajectoryStore
from rlworld.trajectories.object_store import put_blob


def png_frame(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1280, 800), color).save(output, format="PNG")
    return output.getvalue()


def write_steps(writer, frame_digest: str, count: int) -> None:
    for step_index in range(count):
        observation = Observation.model_validate({"step_index": step_index, "screenshot": {
            "uri": f"blob:{frame_digest}", "width": 1280, "height": 800, "sha256": frame_digest}})
        writer.write_step(StepRecord(step_index=step_index, observation=observation))


@pytest.fixture
def stored():
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    frame_digest = put_blob(trajectory_store.object_store, png_frame("navy")).removeprefix("sha256:")
    running = trajectory_store.start_episode(episode("b00000_g00000_s001_a0", sample_index=1), {}, {})
    write_steps(running, frame_digest, 3)
    trajectory_store.start_episode(episode("b00000_g00000_s000_a0"), {}, {})
    finished = trajectory_store.start_episode(episode("b00000_g00000_s002_a0", sample_index=2), {}, {})
    write_steps(finished, frame_digest, 1)
    finished.finish(reward(), {})
    return TrajectoryViews(trajectory_store), frame_digest


def test_episodes_filter_by_status_and_page_their_steps(stored):
    views, _ = stored
    assert [record.episode_id for record in views.episodes("run_1", "running")] == [
        "b00000_g00000_s000_a0", "b00000_g00000_s001_a0"]
    assert [record.episode_id for record in views.episodes("run_1", "finished")] == ["b00000_g00000_s002_a0"]
    followed = views.episode("run_1", "b00000_g00000_s001_a0", first_step=1)
    assert [step.step_index for step in followed.steps] == [1, 2]
    with pytest.raises(UnknownRecordError):
        views.episode("run_1", "missing")


def test_the_live_wall_samples_running_episodes_with_their_latest_frame(stored):
    views, frame_digest = stored
    tiles = views.live_wall("run_1")
    assert [(tile.episode_id, tile.step_index, tile.frame) for tile in tiles] == [
        ("b00000_g00000_s000_a0", None, None), ("b00000_g00000_s001_a0", 2, frame_digest)]
    assert len(views.live_wall("run_1", limit=1)) == 1


def test_frames_come_as_thumbnails_first_and_full_size_on_demand(stored):
    views, frame_digest = stored
    full, media_type = views.frame(frame_digest)
    assert media_type == "image/png" and Image.open(io.BytesIO(full)).size == (1280, 800)
    thumbnail, media_type = views.frame(frame_digest, "thumbnail")
    assert media_type == "image/webp" and Image.open(io.BytesIO(thumbnail)).size == (320, 200)
    assert views.trajectory_store.object_store.exists(f"thumbnails/320x200/{frame_digest}.webp")
    with pytest.raises(UnknownRecordError):
        views.frame("0" * 64)


def test_routes_serve_episodes_the_live_wall_and_frames(stored, example_payload, tmp_path):
    views, frame_digest = stored
    payload = eval_payload(example_payload)
    service = build_experiment_service(payload, tmp_path)
    experiment = service.launch(service.save_spec(dump_spec_document(parse_spec_document(payload))))
    client = create_app(service, trajectory_views=views).test_client()
    assert client.get(f"/v1/experiments/{experiment.experiment_id}").get_json()["run_ids"] == experiment.run_ids
    assert client.get("/v1/experiments/experiment_999999").status_code == 404
    running = client.get("/v1/runs/run_1/episodes?status=running").get_json()["episodes"]
    assert len(running) == 2 and "steps" not in running[0]
    assert client.get("/v1/runs/run_1/episodes?status=paused").status_code == 400
    followed = client.get("/v1/runs/run_1/episodes/b00000_g00000_s001_a0?first_step=2").get_json()
    assert [step["step_index"] for step in followed["steps"]] == [2]
    assert client.get("/v1/runs/run_1/episodes/b00000_g00000_s001_a0?first_step=x").status_code == 400
    assert client.get("/v1/runs/run_1/live?limit=1").get_json()["tiles"][0]["episode_id"] == "b00000_g00000_s000_a0"
    thumbnail = client.get(f"/v1/frames/{frame_digest}?size=thumbnail")
    assert thumbnail.mimetype == "image/webp" and "immutable" in thumbnail.headers["Cache-Control"]
    assert client.get(f"/v1/frames/{'0' * 64}").status_code == 404
    assert create_app(service).test_client().get("/v1/runs/run_1/live").status_code == 503
