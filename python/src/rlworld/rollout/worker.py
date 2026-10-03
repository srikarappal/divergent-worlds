"""Rollout worker (DESIGN.md sections 5, 6.7, 6.10 and 6.12).

A worker plays one run. It picks tasks (the curriculum sampler for RL runs, the
suite in order for eval and data runs), plays each task as a group of forks and
writes every trajectory. RL trainers call collect_batch once per step and pass
the informative groups to their trainer adapter; eval and data runs play every
batch on platform hosted workers. The worker converges to the run's desired
state: a paused run starts no new groups, a stopped run drains and concurrency
and priority changes apply to the next groups. Spend goes to the control plane,
which enforces the budget.
"""

import itertools
import math
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Protocol

from rlworld.gateway.metering import UsageMeter
from rlworld.rollout.control import RunControlError
from rlworld.rollout.episode import TaskInstance, describe
from rlworld.rollout.groups import GroupResult, GroupRunner, RunStoppedError, episode_identifier
from rlworld.services.experiments import TERMINAL_STATES, DryRun, RunRecord
from rlworld.specs.document import SpecDocument
from rlworld.tasks.curriculum import (
    CandidateTask,
    CurriculumSampler,
    PassRateTracker,
    filter_informative_groups,
    record_group_outcomes,
)

DRY_RUN_PRIORITY = "interactive"


class TaskProvider(Protocol):
    """The task registry's view for one run: candidate tasks and their generated instances."""

    def candidates(self) -> Sequence[CandidateTask]: ...

    def instance(self, candidate: CandidateTask) -> TaskInstance: ...

    def expand(self, tracker: PassRateTracker, policy_version: int) -> None: ...


class RunControl(Protocol):
    def run(self, run_id: str) -> RunRecord: ...

    def record_spend(self, run_id: str, amount_usd: float) -> Any: ...

    def publish_metrics(self, run_id: str, metrics: dict[str, Any]) -> Any: ...

    def mark_finished(self, run_id: str, state: Literal["completed", "failed"], detail: str = "") -> Any: ...


class ConcurrencyGate:
    """Bounds envs in flight; a lowered limit applies as groups finish and one group always fits an idle gate."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.in_use = 0
        self.condition = threading.Condition()

    def fits(self, count: int) -> bool:
        return self.in_use == 0 or self.in_use + count <= self.limit

    def set_limit(self, limit: int) -> None:
        with self.condition:
            self.limit = limit
            self.condition.notify_all()

    def try_acquire(self, count: int, timeout_seconds: float) -> bool:
        with self.condition:
            if not self.condition.wait_for(partial(self.fits, count), timeout_seconds):
                return False
            self.in_use += count
            return True

    def release(self, count: int) -> None:
        with self.condition:
            self.in_use -= count
            self.condition.notify_all()


@dataclass
class RunTotals:
    groups: int = 0
    episodes: int = 0
    successes: int = 0
    agent_errors: int = 0
    env_errors: int = 0
    human_intervened: int = 0
    reward_sum: float = 0.0

    def add(self, group: GroupResult) -> None:
        self.groups += 1
        for episode in (*group.episodes, *group.replaced_episodes):
            self.episodes += 1
            if episode.termination == "env_error":
                self.env_errors += 1
                continue
            if episode.human_intervened:
                self.human_intervened += 1
                continue
            self.successes += episode.termination == "success"
            self.agent_errors += episode.termination == "agent_error"
            self.reward_sum += episode.reward.total if episode.reward else 0.0

    def metrics(self) -> dict[str, Any]:
        scored_episodes = self.episodes - self.env_errors - self.human_intervened
        return {
            "groups": self.groups,
            "episodes": self.episodes,
            "env_errors": self.env_errors,
            "human_intervened": self.human_intervened,
            "agent_errors": self.agent_errors,
            "success_rate": self.successes / scored_episodes if scored_episodes else None,
            "mean_reward": self.reward_sum / scored_episodes if scored_episodes else None,
            "env_error_rate": self.env_errors / self.episodes if self.episodes else None,
        }


@dataclass(frozen=True)
class RolloutBatch:
    batch_index: int
    policy_version: int
    groups: tuple[GroupResult, ...]
    informative_groups: tuple[GroupResult, ...] = ()
    zero_advantage_groups: int = 0
    undersized_groups: int = 0


class RolloutWorker:
    def __init__(
        self,
        run_id: str,
        document: SpecDocument,
        group_runner: GroupRunner,
        task_provider: TaskProvider,
        control: RunControl | None = None,
        sampler: CurriculumSampler | None = None,
        meter: UsageMeter | None = None,
        poll_seconds: float = 5.0,
        metrics_interval_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.run_id = run_id
        self.run_spec = document.run
        self.group_runner = group_runner
        self.task_provider = task_provider
        self.control = control
        self.sampler = sampler or CurriculumSampler(PassRateTracker(), document.run.group_size)
        self.meter = meter
        self.poll_seconds = poll_seconds
        self.metrics_interval_seconds = metrics_interval_seconds
        self.clock = clock
        self.sleep = sleep
        self.stop_event = group_runner.stop_event
        self.gate = ConcurrencyGate(document.run.max_concurrent_envs)
        self.desired_state = "running"
        self.priority = document.run.priority
        self.batch_numbers = itertools.count()
        self.totals = RunTotals()
        self.reported_spend_usd = 0.0
        self.polled_at = -math.inf
        self.metrics_published_at = -math.inf
        self.lock = threading.Lock()

    def apply_desired_state(self, run: RunRecord) -> None:
        with self.lock:
            self.desired_state = run.state
            self.priority = run.priority
        self.gate.set_limit(run.desired_concurrency)
        if run.state in TERMINAL_STATES:
            self.stop_event.set()

    def sync_desired_state(self) -> None:
        if self.control is None or self.clock() - self.polled_at < self.poll_seconds:
            return
        self.polled_at = self.clock()
        try:
            run = self.control.run(self.run_id)
        except RunControlError:
            return  # keep the last known state; the next poll tries again
        self.apply_desired_state(run)

    def reserve_envs(self, count: int) -> bool:
        """Waits while the run is paused or at its concurrency limit; False once the run stops."""
        while True:
            self.sync_desired_state()
            if self.stop_event.is_set():
                return False
            if self.desired_state == "paused":
                self.sleep(self.poll_seconds)
            elif self.gate.try_acquire(count, self.poll_seconds):
                return True

    def select_candidates(self, batch_index: int, policy_version: int) -> list[CandidateTask]:
        """RL runs first let the registry grow toward the curriculum's frontier, then sample by pass rate."""
        if self.run_spec.type == "rl":
            self.task_provider.expand(self.sampler.tracker, policy_version)
            candidates = list(self.task_provider.candidates())
            return self.sampler.sample(candidates, self.run_spec.tasks_per_batch, policy_version)
        candidates = list(self.task_provider.candidates())
        first_index = batch_index * self.run_spec.tasks_per_batch
        return candidates[first_index:first_index + self.run_spec.tasks_per_batch]

    def collect_batch(self, policy_version: int = 0) -> RolloutBatch:
        """Plays one batch of groups; RL trainers call this once per training step."""
        batch_index = next(self.batch_numbers)
        candidates = self.select_candidates(batch_index, policy_version)
        futures: list[Future] = []
        with ThreadPoolExecutor(max_workers=max(1, len(candidates))) as pool:
            for group_number, candidate in enumerate(candidates):
                if not self.reserve_envs(self.run_spec.group_size):
                    break
                group_id = f"b{batch_index:05d}_g{group_number:05d}"
                futures.append(pool.submit(self.play_group, candidate, group_id, policy_version))
        groups = tuple(group for group in (future.result() for future in futures) if group is not None)
        batch = self.summarize(batch_index, policy_version, groups)
        self.report_spend()
        self.publish_metrics(force=True, extra={
            "batch_index": batch_index, "policy_version": policy_version, "batch_groups": len(groups),
            "informative_groups": len(batch.informative_groups), "zero_advantage_groups": batch.zero_advantage_groups,
            "undersized_groups": batch.undersized_groups,
        })
        return batch

    def play_group(self, candidate: CandidateTask, group_id: str, policy_version: int) -> GroupResult | None:
        try:
            instance = self.task_provider.instance(candidate)
            group = self.group_runner.run(instance, self.run_spec.group_size, policy_version, group_id, self.priority)
        except RunStoppedError:
            return None
        finally:
            self.gate.release(self.run_spec.group_size)
        if self.run_spec.type == "rl":
            record_group_outcomes(self.sampler.tracker, candidate, policy_version, group.rewards)
        with self.lock:
            self.totals.add(group)
        self.report_spend()
        self.publish_metrics()
        return group

    def summarize(self, batch_index: int, policy_version: int, groups: tuple[GroupResult, ...]) -> RolloutBatch:
        if self.run_spec.type != "rl":
            return RolloutBatch(batch_index, policy_version, groups)
        informative_groups = []
        zero_advantage_groups = undersized_groups = 0
        for group in groups:
            filtered = filter_informative_groups([group.rewards])
            if filtered.kept_groups:
                informative_groups.append(group)
            zero_advantage_groups += filtered.zero_advantage_groups
            undersized_groups += filtered.undersized_groups
        return RolloutBatch(batch_index, policy_version, groups, tuple(informative_groups), zero_advantage_groups,
                            undersized_groups)

    def report_spend(self) -> None:
        if self.control is None or self.meter is None:
            return
        total_usd = sum(summary.cost_usd for summary in self.meter.summary(self.run_id).values())
        with self.lock:
            unreported_usd = total_usd - self.reported_spend_usd
            if unreported_usd <= 0:
                return
            self.reported_spend_usd = total_usd
        try:
            self.control.record_spend(self.run_id, unreported_usd)
        except RunControlError:
            with self.lock:
                self.reported_spend_usd -= unreported_usd

    def gateway_metrics(self) -> dict[str, Any]:
        if self.meter is None:
            return {}
        summaries = self.meter.summary(self.run_id).values()
        return {"rate_limit_wait_seconds": sum(summary.rate_limit_wait_seconds for summary in summaries)}

    def publish_metrics(self, force: bool = False, extra: dict[str, Any] | None = None) -> None:
        if self.control is None:
            return
        with self.lock:
            if not force and self.clock() - self.metrics_published_at < self.metrics_interval_seconds:
                return
            self.metrics_published_at = self.clock()
            metrics = {**self.totals.metrics(), **self.group_runner.episode_runner.step_timings.metrics(),
                       **self.gateway_metrics(), **(extra or {})}
        try:
            self.control.publish_metrics(self.run_id, metrics)
        except RunControlError:
            pass  # metrics are best effort; the next publish carries the totals

    def run(self) -> None:
        """Plays every batch of an eval or data run, then marks the run completed unless it was stopped."""
        try:
            for _ in range(self.run_spec.batches):
                if self.stop_event.is_set() or not self.collect_batch().groups:
                    break
        except Exception as error:  # the run fails visibly instead of hanging in running
            self.finish("failed", describe(error))
            return
        if not self.stop_event.is_set():
            self.finish("completed")

    def finish(self, state: Literal["completed", "failed"], detail: str = "") -> None:
        if self.control is not None:
            self.control.mark_finished(self.run_id, state, detail)

    def play_dry_run(self, candidate: CandidateTask) -> GroupResult:
        return self.group_runner.run(self.task_provider.instance(candidate), 1, 0, self.run_id, DRY_RUN_PRIORITY)

    def start_dry_run(self) -> DryRun:
        """Plays one episode of the first task at interactive priority in the background."""
        candidate = self.task_provider.candidates()[0]
        threading.Thread(target=self.play_dry_run, args=(candidate,), name=f"rollout_{self.run_id}",
                         daemon=True).start()
        return DryRun(run_id=self.run_id, episode_id=episode_identifier(self.run_id, 0, 0))


WorkerFactory = Callable[[str, SpecDocument], RolloutWorker]


class PlatformRunExecutor:
    """experimentd's RunExecutor: platform hosted rollout workers for eval and data runs plus dry runs."""

    def __init__(self, build_worker: WorkerFactory) -> None:
        self.build_worker = build_worker
        self.workers: dict[str, RolloutWorker] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.dry_run_numbers = itertools.count(1)
        self.lock = threading.Lock()

    def start(self, run: RunRecord, document: SpecDocument) -> None:
        worker = self.build_worker(run.run_id, document)
        thread = threading.Thread(target=worker.run, name=f"rollout_{run.run_id}", daemon=True)
        with self.lock:
            self.workers[run.run_id] = worker
            self.threads[run.run_id] = thread
        thread.start()

    def apply(self, run: RunRecord) -> None:
        with self.lock:
            worker = self.workers.get(run.run_id)
        if worker is not None:
            worker.apply_desired_state(run)

    def dry_run(self, document: SpecDocument) -> DryRun:
        return self.build_worker(f"dry_run_{next(self.dry_run_numbers):06d}", document).start_dry_run()

    def wait(self, run_id: str, timeout_seconds: float | None = None) -> bool:
        """Joins a run's worker thread; True once it has ended."""
        with self.lock:
            thread = self.threads[run_id]
        thread.join(timeout_seconds)
        return not thread.is_alive()
