"""Little's law capacity and cost model (DESIGN.md section 8).

Concurrency is throughput times latency. From one run spec and measured
profiles this estimates envs, hosts per pool, policy calls and tokens, policy
GPUs (bounded by prefill compute or KV cache memory, whichever is larger),
judge tokens, screenshot volume, wall clock and cost. Prices default to None:
an estimate never invents a price, it lists what is missing instead.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from rlworld.specs.document import SpecDocument


@dataclass(frozen=True)
class PoolProfile:
    """Measured per pool; the defaults are the planning numbers from DESIGN.md section 6.1."""

    vms_per_host: int
    env_step_seconds: float
    host_hourly_cost_usd: float | None = None


@dataclass(frozen=True)
class PolicyProfile:
    seconds_per_call: float = 3.0
    new_input_tokens_per_step: int = 1_500
    average_context_tokens: int = 20_000
    output_tokens_per_step: int = 150
    prefill_tokens_per_gpu_second: float = 28_500.0
    kv_bytes_per_token: int = 57_344
    kv_bytes_per_gpu: int = 50 * 10**9
    gpu_hourly_cost_usd: float | None = None
    input_price_per_million_usd: float | None = None
    output_price_per_million_usd: float | None = None


@dataclass(frozen=True)
class JudgeProfile:
    input_tokens_per_call: int = 10_000
    output_tokens_per_call: int = 400
    call_fraction: float = 1.0
    cache_hit_fraction: float = 0.0
    input_price_per_million_usd: float | None = None
    output_price_per_million_usd: float | None = None


@dataclass(frozen=True)
class StorageProfile:
    screenshot_bytes: int = 150_000
    retention_months: float = 1.0
    gigabyte_month_price_usd: float | None = None


DEFAULT_POOL_PROFILES: dict[str, PoolProfile] = {
    "firecracker": PoolProfile(vms_per_host=300, env_step_seconds=1.0),
    "qemu_templated": PoolProfile(vms_per_host=100, env_step_seconds=1.5),
    "cloud_windows": PoolProfile(vms_per_host=1, env_step_seconds=2.0),
    "apple_virtualization": PoolProfile(vms_per_host=2, env_step_seconds=1.5),
    "cuttlefish": PoolProfile(vms_per_host=40, env_step_seconds=1.5),
}


@dataclass(frozen=True)
class CapacityEstimate:
    episodes_per_batch: int
    episodes_total: int
    steps_per_episode: int
    seconds_per_step: float
    episode_seconds: float
    concurrent_envs: int
    batch_wall_seconds: float
    wall_clock_hours: float
    policy_calls_per_second: float
    hosts_by_pool: dict[str, int]
    new_input_tokens_per_second: float
    full_context_tokens_per_second: float
    policy_gpus: int
    policy_gpus_if_cache_breaks: int
    frontier_input_tokens_per_minute: float
    judge_calls: int
    judge_input_tokens: int
    screenshot_bytes_total: int
    screenshot_megabytes_per_second: float
    cost_usd_by_item: dict[str, float] = field(default_factory=dict)
    missing_prices: tuple[str, ...] = ()

    @property
    def estimated_cost_usd(self) -> float | None:
        """None while any price is missing, so a partial sum is never mistaken for the total."""
        return None if self.missing_prices else round(sum(self.cost_usd_by_item.values()), 2)


def expected_steps(document: SpecDocument, step_overhead: float) -> int:
    return min(document.task.max_steps, math.ceil(document.task.difficulty.oracle_steps * step_overhead))


def token_cost(token_count: float, price_per_million: float | None) -> float | None:
    return None if price_per_million is None else token_count * price_per_million / 1_000_000


def estimate_capacity(
    document: SpecDocument,
    policy_profile: PolicyProfile | None = None,
    judge_profile: JudgeProfile | None = None,
    pool_profiles: Mapping[str, PoolProfile] | None = None,
    storage_profile: StorageProfile | None = None,
    policy_is_frontier: bool = False,
    concurrency_cap: int | None = None,
    step_overhead: float = 1.0,
) -> CapacityEstimate:
    policy = policy_profile or PolicyProfile()
    judge = judge_profile or JudgeProfile()
    storage = storage_profile or StorageProfile()
    pool_name = document.image.pool.vmm
    pool = (pool_profiles or DEFAULT_POOL_PROFILES)[pool_name]
    run = document.run

    episodes_per_batch = run.tasks_per_batch * run.group_size
    episodes_total = episodes_per_batch * run.batches
    steps_per_episode = expected_steps(document, step_overhead)
    seconds_per_step = policy.seconds_per_call + pool.env_step_seconds
    episode_seconds = steps_per_episode * seconds_per_step
    concurrent_envs = max(1, min(run.max_concurrent_envs, episodes_per_batch, concurrency_cap or episodes_per_batch))
    batch_wall_seconds = math.ceil(episodes_per_batch / concurrent_envs) * episode_seconds
    wall_clock_hours = batch_wall_seconds * run.batches / 3600
    policy_calls_per_second = concurrent_envs / seconds_per_step
    hosts = math.ceil(concurrent_envs / pool.vms_per_host)
    new_input_tokens_per_second = policy_calls_per_second * policy.new_input_tokens_per_step
    full_context_tokens_per_second = policy_calls_per_second * policy.average_context_tokens

    policy_gpus = 0
    policy_gpus_if_cache_breaks = 0
    frontier_input_tokens_per_minute = 0.0
    if policy_is_frontier:
        frontier_input_tokens_per_minute = full_context_tokens_per_second * 60
    else:
        kv_cache_gpus = math.ceil(
            concurrent_envs * policy.average_context_tokens * policy.kv_bytes_per_token / policy.kv_bytes_per_gpu
        )
        policy_gpus = max(kv_cache_gpus, math.ceil(new_input_tokens_per_second / policy.prefill_tokens_per_gpu_second))
        policy_gpus_if_cache_breaks = max(
            kv_cache_gpus, math.ceil(full_context_tokens_per_second / policy.prefill_tokens_per_gpu_second)
        )

    judge_calls = 0
    if document.task.judge_rubric:
        judge_calls = math.ceil(episodes_total * judge.call_fraction * (1 - judge.cache_hit_fraction))
    total_steps = episodes_total * steps_per_episode
    screenshot_bytes_total = total_steps * storage.screenshot_bytes

    priced_items = {
        f"{pool_name}_hosts": (
            None if pool.host_hourly_cost_usd is None else hosts * wall_clock_hours * pool.host_hourly_cost_usd
        ),
        "screenshot_storage": (
            None
            if storage.gigabyte_month_price_usd is None
            else screenshot_bytes_total / 1e9 * storage.retention_months * storage.gigabyte_month_price_usd
        ),
    }
    if policy_is_frontier:
        input_cost = token_cost(total_steps * policy.average_context_tokens, policy.input_price_per_million_usd)
        output_cost = token_cost(total_steps * policy.output_tokens_per_step, policy.output_price_per_million_usd)
        priced_items["frontier_policy_tokens"] = (
            None if input_cost is None or output_cost is None else input_cost + output_cost
        )
    else:
        priced_items["policy_gpus"] = (
            None if policy.gpu_hourly_cost_usd is None else policy_gpus * wall_clock_hours * policy.gpu_hourly_cost_usd
        )
    if judge_calls:
        judge_input_cost = token_cost(judge_calls * judge.input_tokens_per_call, judge.input_price_per_million_usd)
        judge_output_cost = token_cost(judge_calls * judge.output_tokens_per_call, judge.output_price_per_million_usd)
        priced_items["judge_tokens"] = (
            None if judge_input_cost is None or judge_output_cost is None else judge_input_cost + judge_output_cost
        )

    return CapacityEstimate(
        episodes_per_batch=episodes_per_batch,
        episodes_total=episodes_total,
        steps_per_episode=steps_per_episode,
        seconds_per_step=seconds_per_step,
        episode_seconds=episode_seconds,
        concurrent_envs=concurrent_envs,
        batch_wall_seconds=batch_wall_seconds,
        wall_clock_hours=wall_clock_hours,
        policy_calls_per_second=policy_calls_per_second,
        hosts_by_pool={pool_name: hosts},
        new_input_tokens_per_second=new_input_tokens_per_second,
        full_context_tokens_per_second=full_context_tokens_per_second,
        policy_gpus=policy_gpus,
        policy_gpus_if_cache_breaks=policy_gpus_if_cache_breaks,
        frontier_input_tokens_per_minute=frontier_input_tokens_per_minute,
        judge_calls=judge_calls,
        judge_input_tokens=judge_calls * judge.input_tokens_per_call,
        screenshot_bytes_total=screenshot_bytes_total,
        screenshot_megabytes_per_second=policy_calls_per_second * storage.screenshot_bytes / 1e6,
        cost_usd_by_item={name: cost for name, cost in priced_items.items() if cost is not None},
        missing_prices=tuple(sorted(name for name, cost in priced_items.items() if cost is None)),
    )
