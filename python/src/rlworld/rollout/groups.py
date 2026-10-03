"""GRPO groups as forks of one task start state (DESIGN.md sections 3 and 6.12).

One env is leased from the task's snapshot, the task setup is injected and fork
clones that start state for the other members on the same host, so every
member starts identically and shares memory pages. Each member then gets its
own freshly minted sessions, license tokens and secrets, which a fork must
never share. Members that end in an env error are replaced by forks of a new,
identical start state, up to a retry limit. Admission rejections and full
hosts are back pressure, so the group waits for capacity instead of failing.
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from rlworld.entitlements.broker import (
    EntitlementRequest,
    PoolExhaustedError,
    UnknownPoolError,
    guest_injection_payload,
)
from rlworld.rewards.records import RewardRecord
from rlworld.rollout.episode import (
    FLEET_FAILURES,
    EntitlementSource,
    EnvFleet,
    EpisodePlan,
    EpisodeRunner,
    TaskInstance,
    describe,
)
from rlworld.sdk.client import AdmissionRejectedError, LeaseGroupRequest, LeaseInfo, NoCapacityError
from rlworld.trajectories.records import EpisodeRecord

BACKPRESSURE_ERRORS = (AdmissionRejectedError, NoCapacityError)


class RunStoppedError(Exception):
    """The run stopped while a group waited for fleet capacity."""


class GroupSetupError(Exception):
    """The task start state or a member's entitlements could not be prepared."""


@dataclass(frozen=True)
class EnvShape:
    """Pool and per VM resources of a run's envs, from the image and its pool profile."""

    pool: str
    memory_mib: int
    vcpus: int
    cpu_millicores: int = 2000
    host_scoped_licenses: tuple[str, ...] = ()
    lease_ttl_seconds: int = 300


@dataclass(frozen=True)
class LeaseSettings:
    run_id: str
    tenant: str
    cost_center: str
    policy_endpoint: str
    shape: EnvShape
    licenses: tuple[str, ...] = ()
    world_time: str | None = None


@dataclass(frozen=True)
class GroupResult:
    group_id: str
    instance: TaskInstance
    episodes: tuple[EpisodeRecord, ...]
    replaced_episodes: tuple[EpisodeRecord, ...] = ()

    @property
    def rewards(self) -> tuple[RewardRecord, ...]:
        return tuple(episode.reward for episode in self.episodes if episode.reward is not None)


def episode_identifier(group_id: str, sample_index: int, attempt: int) -> str:
    return f"{group_id}_s{sample_index:03d}_a{attempt}"


def is_env_error(episode: EpisodeRecord) -> bool:
    return episode.termination == "env_error"


class GroupRunner:
    def __init__(
        self,
        fleet: EnvFleet,
        episode_runner: EpisodeRunner,
        settings: LeaseSettings,
        entitlements: EntitlementSource | None = None,
        env_error_retries: int = 1,
        backoff_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.fleet = fleet
        self.episode_runner = episode_runner
        self.settings = settings
        self.entitlements = entitlements
        self.env_error_retries = env_error_retries
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        self.stop_event = threading.Event()

    def run(self, instance: TaskInstance, group_size: int, policy_version: int, group_id: str,
            priority: str) -> GroupResult:
        episodes: dict[int, EpisodeRecord] = {}
        replaced: list[EpisodeRecord] = []
        for attempt in range(self.env_error_retries + 1):
            pending = [index for index in range(group_size) if index not in episodes or is_env_error(episodes[index])]
            if not pending:
                break
            plans = [EpisodePlan(episode_identifier(group_id, index, attempt), instance, policy_version, group_id,
                                 index) for index in pending]
            for plan, episode in zip(plans, self.play_members(plans, priority)):
                if plan.sample_index in episodes:
                    replaced.append(episodes[plan.sample_index])
                episodes[plan.sample_index] = episode
        return GroupResult(group_id, instance, tuple(episodes[index] for index in range(group_size)), tuple(replaced))

    def play_members(self, plans: list[EpisodePlan], priority: str) -> list[EpisodeRecord]:
        try:
            leases = self.lease_start_state(plans[0].instance, len(plans), priority)
        except GroupSetupError as error:
            return [self.episode_runner.record_env_error(plan, str(error)) for plan in plans]
        with ThreadPoolExecutor(max_workers=len(plans)) as pool:
            return list(pool.map(self.play_member, plans, leases))

    def play_member(self, plan: EpisodePlan, lease: LeaseInfo) -> EpisodeRecord:
        try:
            entitlement_lease_id = self.grant_entitlements(plan, lease)
        except GroupSetupError as error:
            self.close_quietly(lease.lease_id)
            return self.episode_runner.record_env_error(plan, str(error))
        try:
            return self.episode_runner.run(plan, lease, entitlement_lease_id)
        finally:
            if self.entitlements is not None and entitlement_lease_id is not None:
                self.entitlements.release(entitlement_lease_id)

    def lease_start_state(self, instance: TaskInstance, count: int, priority: str) -> list[LeaseInfo]:
        """One env at the task start state, then count - 1 forks of it."""
        shape = self.settings.shape
        request = LeaseGroupRequest(
            run_id=self.settings.run_id,
            tenant=self.settings.tenant,
            pool=shape.pool,
            priority=priority,
            endpoint=self.settings.policy_endpoint,
            snapshot=instance.snapshot,
            memory_mib=shape.memory_mib,
            vcpus=shape.vcpus,
            group_size=1,
            cpu_millicores=shape.cpu_millicores,
            cost_center=self.settings.cost_center,
            ttl_seconds=shape.lease_ttl_seconds,
            host_scoped_licenses=shape.host_scoped_licenses,
            world_time=self.settings.world_time,
            observation_kinds=tuple(instance.task.observation),
        )
        source = self.call_with_backpressure(self.fleet.lease_group, request)[0]
        try:
            if instance.setup_payload:
                self.inject(source.lease_id, instance.setup_payload, "task setup")
            forks = self.call_with_backpressure(self.fleet.fork, source.lease_id, count - 1) if count > 1 else []
        except (GroupSetupError, RunStoppedError):
            self.close_quietly(source.lease_id)
            raise
        return [source, *forks]

    def grant_entitlements(self, plan: EpisodePlan, lease: LeaseInfo) -> str | None:
        """Fresh sessions, license tokens and secrets for one member, injected after its restore or fork."""
        instance = plan.instance
        if not (self.settings.licenses or instance.personas or instance.secrets):
            return None
        if self.entitlements is None:
            raise GroupSetupError("the task needs entitlements but no broker is configured")
        request = EntitlementRequest(episode_id=plan.episode_id, run_id=self.settings.run_id,
                                     tenant=self.settings.tenant, host_id=lease.host_id,
                                     licenses=self.settings.licenses, personas=instance.personas,
                                     secrets=instance.secrets)
        try:
            granted = self.entitlements.acquire(request)
        except (PoolExhaustedError, UnknownPoolError) as error:
            raise GroupSetupError(f"entitlements unavailable: {describe(error)}") from error
        try:
            self.inject(lease.lease_id, guest_injection_payload(granted), "entitlements")
        except GroupSetupError:
            self.entitlements.release(granted.lease_id)
            raise
        return granted.lease_id

    def inject(self, lease_id: str, payload: dict[str, Any], description: str) -> None:
        try:
            self.fleet.inject(lease_id, payload)
        except FLEET_FAILURES as error:
            raise GroupSetupError(f"injecting {description} failed: {describe(error)}") from error

    def call_with_backpressure(self, fleet_call: Callable[..., list[LeaseInfo]], *arguments: Any) -> list[LeaseInfo]:
        while True:
            if self.stop_event.is_set():
                raise RunStoppedError("the run stopped while waiting for fleet capacity")
            try:
                return fleet_call(*arguments)
            except BACKPRESSURE_ERRORS:
                self.sleep(self.backoff_seconds)
            except FLEET_FAILURES as error:
                raise GroupSetupError(f"{fleet_call.__name__} failed: {describe(error)}") from error

    def close_quietly(self, lease_id: str) -> None:
        try:
            self.fleet.close(lease_id)
        except FLEET_FAILURES:
            pass  # the reaper ends leases that cannot be closed
