import pytest
from support.trajectory_fakes import episode, reward, step

from rlworld.trajectories import (
    BlobIntegrityError,
    FilesystemObjectStore,
    InMemoryObjectStore,
    TrajectoryStore,
    get_blob,
    put_blob,
)
from rlworld.trajectories.object_store import blob_key
from rlworld.trajectories.trajectory_store import first_free_number


@pytest.fixture(params=["memory", "filesystem"])
def object_store(request, tmp_path):
    return InMemoryObjectStore() if request.param == "memory" else FilesystemObjectStore(tmp_path / "store")


def test_object_store_basics(object_store):
    object_store.put_bytes("runs/run_1/a.json", b"one")
    object_store.put_bytes("runs/run_1/b.json", b"two")
    object_store.put_bytes("runs/run_2/c.json", b"three")
    assert object_store.get_bytes("runs/run_1/a.json") == b"one"
    assert object_store.list_keys("runs/run_1/") == ["runs/run_1/a.json", "runs/run_1/b.json"]
    object_store.delete("runs/run_1/a.json")
    assert not object_store.exists("runs/run_1/a.json")
    with pytest.raises(KeyError):
        object_store.get_bytes("runs/run_1/a.json")


@pytest.mark.parametrize("bad_key", ["../escape", "/absolute", "runs//double", "runs/with space", "runs/."])
def test_unsafe_keys_are_rejected(object_store, bad_key):
    with pytest.raises(ValueError):
        object_store.put_bytes(bad_key, b"x")


def test_blobs_are_content_addressed_and_verified(object_store):
    first_digest = put_blob(object_store, b"screenshot bytes")
    assert put_blob(object_store, b"screenshot bytes") == first_digest
    assert get_blob(object_store, first_digest) == b"screenshot bytes"
    object_store.put_bytes(blob_key(first_digest), b"tampered")
    with pytest.raises(BlobIntegrityError):
        get_blob(object_store, first_digest)


def test_an_episode_round_trips_with_steps(object_store):
    trajectory_store = TrajectoryStore(object_store)
    writer = trajectory_store.start_episode(episode(), {"files": {}}, {"total": 1250})
    running_episode = trajectory_store.load_episode("run_1", "episode_1")
    assert running_episode.reward is None and running_episode.steps == ()
    writer.write_step(step(0))
    writer.write_step(step(1, actions=[{"name": "done"}]))
    finished = writer.finish(reward(), {"files": {"report.xlsx": {}}}, final_snapshot_ref="snapshots/abc")
    loaded = trajectory_store.load_episode("run_1", "episode_1")
    assert loaded.reward == finished.reward
    assert [loaded_step.step_index for loaded_step in loaded.steps] == [0, 1]
    assert loaded.steps[1].actions == ({"name": "done"},)
    assert trajectory_store.load_state(loaded.initial_state_ref) == {"files": {}}
    assert trajectory_store.load_state(loaded.final_state_ref) == {"files": {"report.xlsx": {}}}
    assert trajectory_store.load_state(loaded.ground_truth_ref) == {"total": 1250}
    assert loaded.finished_at is not None
    assert trajectory_store.load_episode("run_1", "episode_1", with_steps=False).steps == ()


def test_steps_must_arrive_in_order(object_store):
    writer = TrajectoryStore(object_store).start_episode(episode(), {}, {})
    with pytest.raises(ValueError):
        writer.write_step(step(1))


def test_listing_episodes_per_run(object_store):
    trajectory_store = TrajectoryStore(object_store)
    for episode_id in ("episode_b", "episode_a"):
        trajectory_store.start_episode(episode(episode_id=episode_id), {}, {}).finish(reward(), {})
    trajectory_store.start_episode(episode(episode_id="other", run_id="run_2"), {}, {})
    assert trajectory_store.list_episode_ids("run_1") == ["episode_a", "episode_b"]
    assert [loaded.episode_id for loaded in trajectory_store.iter_episodes("run_1")] == ["episode_a", "episode_b"]
    with pytest.raises(ValueError):
        trajectory_store.list_episode_ids("../run_1")


def test_saved_runs_list_and_new_ids_continue_after_the_highest_saved_one(object_store):
    for run_id in ("dry_run_000004", "experiment_000002_run_001", "dry_run_000011"):
        object_store.put_bytes(f"runs/{run_id}/episodes/{run_id}_s000_a0/episode.json", b"{}")
    run_ids = TrajectoryStore(object_store).run_ids()
    assert run_ids == ["dry_run_000004", "dry_run_000011", "experiment_000002_run_001"]
    assert first_free_number(run_ids, "dry_run") == 12
    assert first_free_number(run_ids, "experiment") == 3
    assert first_free_number(run_ids, "gate_rerun") == 1
