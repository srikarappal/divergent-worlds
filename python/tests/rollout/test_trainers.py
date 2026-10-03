import pytest
from support.rollout_fakes import rollout_document, task_instance
from support.trajectory_fakes import episode, reward

from rlworld.actions.models import Observation
from rlworld.gateway.messages import TokenTrace
from rlworld.rollout import (
    GroupResult,
    areal_rows,
    prime_rl_training_samples,
    skyrl_generator_output,
    turn_samples,
    verl_agent_loop_outputs,
    within_staleness,
)
from rlworld.trajectories import InMemoryObjectStore, ModelTurn, StepRecord, TrajectoryStore


def store_episode(trajectory_store, episode_id, sample_index, total, termination, turns, logprobs=(-0.5, -0.25)):
    writer = trajectory_store.start_episode(episode(episode_id, sample_index=sample_index), {}, {})
    for turn_index in range(turns):
        prompt_ref = writer.put_prompt([{"role": "user", "content": [
            {"type": "text", "text": f"Step {turn_index}."},
            {"type": "image", "uri": f"s3://frames/{episode_id}_{turn_index}.webp"}]}])
        trace = TokenTrace(prompt_token_ids=(1, 2, 3), completion_token_ids=(10 + turn_index, 20 + turn_index),
                           completion_logprobs=logprobs)
        writer.write_step(StepRecord(step_index=turn_index, observation=Observation(step_index=turn_index),
                                     model_turn=ModelTurn(prompt_ref=prompt_ref, token_trace=trace)))
    return writer.finish(reward(total, termination, counts_for_training=termination != "env_error"), {})


@pytest.fixture
def stored_group(example_payload):
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    episodes = (
        store_episode(trajectory_store, "episode_0", 0, 1.0, "success", 2),
        store_episode(trajectory_store, "episode_1", 1, 0.0, "failure", 1),
        store_episode(trajectory_store, "episode_2", 2, 0.0, "env_error", 1),
    )
    instance = task_instance(rollout_document(example_payload))
    return GroupResult("b00000_g00000", instance, episodes), trajectory_store


def test_every_turn_of_a_trainable_episode_is_a_sample(stored_group):
    group, trajectory_store = stored_group
    samples = turn_samples(group, trajectory_store)
    assert [(sample.episode_id, sample.turn_index, sample.is_last_turn) for sample in samples] == [
        ("episode_0", 0, False), ("episode_0", 1, True), ("episode_1", 0, True)]
    assert [sample.advantage for sample in samples] == [0.5, 0.5, -0.5]
    assert samples[1].completion_token_ids == (11, 21) and samples[1].policy_version == 3
    assert samples[0].image_uris == ("s3://frames/episode_0_0.webp",)


def test_logprobs_must_cover_every_completion_token(example_payload):
    trajectory_store = TrajectoryStore(InMemoryObjectStore())
    broken = store_episode(trajectory_store, "episode_0", 0, 1.0, "success", 1, logprobs=(-0.5,))
    group = GroupResult("b00000_g00000", task_instance(rollout_document(example_payload)), (broken,))
    with pytest.raises(ValueError, match="1 logprobs for 2 tokens"):
        turn_samples(group, trajectory_store)


def test_shims_lay_out_each_trainer_format(stored_group):
    samples = turn_samples(*stored_group)
    verl = verl_agent_loop_outputs(samples)[0]
    assert verl["response_ids"] == [10, 20] and verl["response_mask"] == [1, 1]
    assert verl["response_logprobs"] == [-0.5, -0.25] and verl["reward_score"] == 1.0
    assert verl["extra_fields"] == {"min_global_steps": 3, "max_global_steps": 3}
    skyrl = skyrl_generator_output(samples)
    assert skyrl["rewards"] == [0.0, 1.0, 0.0] and skyrl["is_last_step"] == [False, True, True]
    assert skyrl["trajectory_ids"][2] == {"instance_id": "b00000_g00000", "repetition_id": 1}
    assert skyrl["loss_masks"][0] == [1, 1] and skyrl["prompt_token_ids"][0] == [1, 2, 3]
    prime = prime_rl_training_samples(samples, temperature=1.0)[0]
    assert prime["token_ids"] == [1, 2, 3, 10, 20]
    assert prime["mask"] == [False, False, False, True, True]
    assert prime["logprobs"] == [0.0, 0.0, 0.0, -0.5, -0.25]
    assert prime["advantages"] == [0.0, 0.0, 0.0, 0.5, 0.5] and len(prime["temperatures"]) == 5
    assert prime["env_name"] == "tasks.finance.reconcile_invoices"
    areal = areal_rows(samples)[2]
    assert areal["input_ids"] == [1, 2, 3, 10, 20] and areal["loss_mask"] == [0, 0, 0, 1, 1]
    assert areal["versions"] == [-1, -1, -1, 3, 3] and areal["rewards"] == 0.0
    assert areal["attention_mask"] == [True] * 5


def test_stale_samples_are_dropped(stored_group):
    samples = turn_samples(*stored_group)
    assert within_staleness(samples, trainer_version=5, max_staleness=1) == []
    assert within_staleness(samples, trainer_version=5, max_staleness=2) == samples
