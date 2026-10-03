import pytest

from rlworld.preflight import JudgeProfile, PolicyProfile, PoolProfile, StorageProfile, estimate_capacity
from rlworld.specs import parse_spec_document, set_path


def test_the_design_worked_example(worked_example_document):
    estimate = estimate_capacity(worked_example_document)
    assert (estimate.episodes_per_batch, estimate.episodes_total) == (4096, 819_200)
    assert (estimate.steps_per_episode, estimate.seconds_per_step, estimate.episode_seconds) == (25, 4.0, 100.0)
    assert estimate.concurrent_envs == 4000
    assert estimate.policy_calls_per_second == 1000
    assert estimate.hosts_by_pool == {"firecracker": 14}
    assert estimate.new_input_tokens_per_second == 1_500_000
    assert estimate.full_context_tokens_per_second == 20_000_000
    assert estimate.policy_gpus == 92
    assert estimate.policy_gpus_if_cache_breaks == 702
    assert estimate.judge_calls == 819_200
    assert estimate.judge_input_tokens / 200 == pytest.approx(40_960_000)
    assert estimate.batch_wall_seconds == 200
    assert estimate.wall_clock_hours == pytest.approx(200 * 200 / 3600)
    assert estimate.screenshot_megabytes_per_second == 150


def test_frontier_policies_are_counted_in_tokens_per_minute(worked_example_document):
    estimate = estimate_capacity(worked_example_document, policy_is_frontier=True)
    assert estimate.frontier_input_tokens_per_minute == 1_200_000_000
    assert estimate.policy_gpus == 0


def test_cost_needs_every_price(worked_example_document):
    unpriced = estimate_capacity(worked_example_document)
    assert unpriced.estimated_cost_usd is None
    assert unpriced.missing_prices == ("firecracker_hosts", "judge_tokens", "policy_gpus", "screenshot_storage")
    priced = estimate_capacity(
        worked_example_document,
        policy_profile=PolicyProfile(gpu_hourly_cost_usd=2.0),
        judge_profile=JudgeProfile(input_price_per_million_usd=1.0, output_price_per_million_usd=2.0),
        pool_profiles={"firecracker": PoolProfile(vms_per_host=300, env_step_seconds=1.0, host_hourly_cost_usd=5.0)},
        storage_profile=StorageProfile(gigabyte_month_price_usd=0.02),
    )
    wall_hours = 200 * 200 / 3600
    assert priced.cost_usd_by_item["firecracker_hosts"] == pytest.approx(14 * wall_hours * 5.0)
    assert priced.cost_usd_by_item["policy_gpus"] == pytest.approx(92 * wall_hours * 2.0)
    assert priced.cost_usd_by_item["judge_tokens"] == pytest.approx(8_192 + 819_200 * 400 * 2.0 / 1e6)
    assert priced.cost_usd_by_item["screenshot_storage"] == pytest.approx(819_200 * 25 * 150_000 / 1e9 * 0.02)
    assert priced.estimated_cost_usd == pytest.approx(sum(priced.cost_usd_by_item.values()), abs=0.01)


def test_small_runs_and_caps_bound_concurrency(worked_example_raw, worked_example_document):
    small_payload = set_path(worked_example_raw, "run.tasks_per_batch", 10)
    small_payload = set_path(small_payload, "run.group_size", 2)
    assert estimate_capacity(parse_spec_document(small_payload)).concurrent_envs == 20
    capped = estimate_capacity(worked_example_document, concurrency_cap=1000)
    assert capped.concurrent_envs == 1000
    assert capped.batch_wall_seconds == 5 * 100


def test_steps_follow_the_oracle_with_overhead_up_to_the_budget(worked_example_document):
    assert estimate_capacity(worked_example_document, step_overhead=1.5).steps_per_episode == 38
    assert estimate_capacity(worked_example_document, step_overhead=3.0).steps_per_episode == 50


def test_tasks_without_a_rubric_cost_no_judge_calls(example_payload):
    document = parse_spec_document(set_path(set_path(example_payload, "task.judge_rubric", None), "run.judge", None))
    estimate = estimate_capacity(document)
    assert estimate.judge_calls == 0
    assert "judge_tokens" not in estimate.missing_prices
