"""Experiment service, the core of experimentd (DESIGN.md sections 6.7, 6.14 and appendix A).

Specs are the only source of truth: saving one returns an immutable digest.
Launch preflights again, expands the sweep into runs and starts each one: eval
and data runs on platform hosted rollout workers, RL runs as trainer jobs.
Live controls change only a run's desired operational state (pause, resume,
stop, concurrency, priority, budget); executors and the trainer's rollout
workers converge to it. Changing tasks, endpoints or rewards means a new run.
"""

import hashlib
import hmac
import itertools
import threading
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from rlworld.preflight.preflight import ExperimentPreflight, PreflightContext, run_experiment_preflight
from rlworld.services.auth import DEFAULT_TENANT
from rlworld.services.trainer_launch import LaunchContext, TrainerLauncher
from rlworld.specs.digest import document_digest, run_spec_digest
from rlworld.specs.document import SpecDocument, load_spec_document
from rlworld.specs.sweep import expand_sweep

RunState = Literal["awaiting_approval", "running", "paused", "stopped", "completed", "failed"]
TERMINAL_STATES = frozenset({"stopped", "completed", "failed"})
PRIORITIES = ("interactive", "eval", "training")


class LaunchBlockedError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class ControlError(ValueError):
    """A control the run cannot take, such as a semantic change to a running run."""


class InvalidRunTokenError(ValueError):
    """A run token that is forged, expired or for a run that has ended."""


class UnknownRecordError(KeyError):
    """No spec, experiment or run has that id."""


class RunEvent(BaseModel):
    sequence: int
    at: float
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    experiment_id: str
    tenant: str = DEFAULT_TENANT
    run_name: str
    spec_digest: str
    run_digest: str
    run_type: Literal["rl", "eval", "data"]
    assignments: dict[str, Any]
    state: RunState
    desired_concurrency: int
    priority: str
    job_id: str | None = None
    created_at: float


class ExperimentRecord(BaseModel):
    experiment_id: str
    tenant: str = DEFAULT_TENANT
    spec_digest: str
    name: str
    budget_usd: float
    spent_usd: float = 0.0
    estimated_cost_usd: float | None = None
    run_ids: list[str]
    approved: bool


class DryRun(BaseModel):
    """One episode played end to end before launch, watched live at its run and episode id."""

    run_id: str
    episode_id: str


class RunExecutor(Protocol):
    """Platform hosted rollout workers for eval and data runs."""

    def start(self, run: RunRecord, document: SpecDocument) -> None: ...

    def apply(self, run: RunRecord) -> None: ...

    def dry_run(self, document: SpecDocument) -> DryRun: ...


class RunSnapshotPreparer(Protocol):
    """Pins a run's snapshot and prefetches it before the run starts."""

    def prepare(self, run: RunRecord, document: SpecDocument) -> dict[str, Any]: ...


class ExperimentService:
    def __init__(
        self,
        preflight_context: PreflightContext,
        executor: RunExecutor,
        trainer_launcher: TrainerLauncher,
        token_secret: bytes,
        fleet_url: str,
        experimentd_url: str,
        approval_threshold_usd: float | None = None,
        token_ttl_seconds: float = 7 * 86_400,
        clock: Callable[[], float] = time.time,
        run_snapshots: RunSnapshotPreparer | None = None,
        first_experiment_number: int = 1,
    ) -> None:
        if not token_secret:
            raise ValueError("run tokens need a signing secret")
        self.preflight_context = preflight_context
        self.executor = executor
        self.trainer_launcher = trainer_launcher
        self.token_secret = token_secret
        self.fleet_url = fleet_url
        self.experimentd_url = experimentd_url
        self.approval_threshold_usd = approval_threshold_usd
        self.token_ttl_seconds = token_ttl_seconds
        self.clock = clock
        self.run_snapshots = run_snapshots
        self.specs: dict[str, tuple[str, SpecDocument]] = {}
        self.experiments: dict[str, ExperimentRecord] = {}
        self.runs: dict[str, RunRecord] = {}
        self.run_documents: dict[str, SpecDocument] = {}
        self.events: dict[str, list[RunEvent]] = {}
        self.experiment_counter = itertools.count(first_experiment_number)
        self.lock = threading.RLock()

    def save_spec(self, yaml_text: str) -> str:
        document = load_spec_document(yaml_text)
        digest = document_digest(document)
        with self.lock:
            self.specs.setdefault(digest, (yaml_text, document))
        return digest

    def spec(self, digest: str) -> tuple[str, SpecDocument]:
        with self.lock:
            if digest not in self.specs:
                raise UnknownRecordError(digest)
            return self.specs[digest]

    def preflight(self, digest: str) -> ExperimentPreflight:
        return run_experiment_preflight(self.spec(digest)[1], self.preflight_context)

    def dry_run(self, digest: str) -> DryRun:
        """One episode of the first sweep point, to watch live before the budget is committed."""
        first_point = expand_sweep(self.spec(digest)[1])[0]
        return self.executor.dry_run(first_point.document)

    def record_event(self, run_id: str, kind: str, detail: dict[str, Any] | None = None) -> None:
        with self.lock:
            run_events = self.events.setdefault(run_id, [])
            run_events.append(RunEvent(sequence=len(run_events) + 1, at=self.clock(), kind=kind, detail=detail or {}))

    def launch(self, digest: str, budget_usd: float | None = None, tenant: str = DEFAULT_TENANT) -> ExperimentRecord:
        document = self.spec(digest)[1]
        report = self.preflight(digest)
        if not report.is_valid:
            raise LaunchBlockedError(report.errors)
        budget = budget_usd if budget_usd is not None else document.experiment.budget_usd
        if report.estimated_cost_usd is not None and report.estimated_cost_usd > budget:
            raise LaunchBlockedError([f"estimated cost {report.estimated_cost_usd:,.2f} USD exceeds {budget:,.2f} USD"])
        needs_approval = self.approval_threshold_usd is not None and (
            report.estimated_cost_usd is None or report.estimated_cost_usd > self.approval_threshold_usd
        )
        experiment_id = f"experiment_{next(self.experiment_counter):06d}"
        run_ids = []
        for point in expand_sweep(document):
            run_id = f"{experiment_id}_run_{point.index:03d}"
            run_ids.append(run_id)
            with self.lock:
                self.run_documents[run_id] = point.document
                self.runs[run_id] = RunRecord(
                    run_id=run_id, experiment_id=experiment_id, tenant=tenant, run_name=point.run_name,
                    spec_digest=document_digest(point.document), run_digest=run_spec_digest(point.document),
                    run_type=point.document.run.type, assignments=point.assignments, state="awaiting_approval",
                    desired_concurrency=point.document.run.max_concurrent_envs,
                    priority=point.document.run.priority, created_at=self.clock(),
                )
        experiment = ExperimentRecord(experiment_id=experiment_id, tenant=tenant, spec_digest=digest,
                                      name=document.experiment.name,
                                      budget_usd=budget, estimated_cost_usd=report.estimated_cost_usd,
                                      run_ids=run_ids, approved=not needs_approval)
        with self.lock:
            self.experiments[experiment_id] = experiment
        if not needs_approval:
            self.start_runs(experiment)
        return self.experiment(experiment_id)

    def approve(self, experiment_id: str) -> ExperimentRecord:
        experiment = self.experiment(experiment_id)
        if not experiment.approved:
            with self.lock:
                experiment.approved = True
            self.start_runs(experiment)
        return self.experiment(experiment_id)

    def start_runs(self, experiment: ExperimentRecord) -> None:
        for run_id in experiment.run_ids:
            run = self.run(run_id)
            document = self.run_documents[run_id]
            with self.lock:
                run.state = "running"  # before starting, so a fast worker's completion is never overwritten
            try:
                if self.run_snapshots is not None:
                    self.record_event(run_id, "snapshot", self.run_snapshots.prepare(run, document))
                if run.run_type == "rl":
                    launch_context = LaunchContext(
                        run_id=run_id, tenant=run.tenant, cost_center=document.experiment.name,
                        fleet_url=self.fleet_url, experimentd_url=self.experimentd_url,
                        policy_endpoint=document.run.policy.endpoint, run_token=self.issue_run_token(run_id),
                        token_file_path=f"/run/rlworld/{run_id}.token",
                    )
                    job_id, _ = self.trainer_launcher.launch(document.run.trainer.launcher,
                                                             document.run.trainer.template, launch_context)
                    with self.lock:
                        run.job_id = job_id
                else:
                    self.executor.start(run, document)
            except Exception as error:  # one run failing to launch never blocks its siblings
                with self.lock:
                    run.state = "failed"
                self.record_event(run_id, "launch_failed", {"error": f"{type(error).__name__}: {error}"})
                continue
            self.record_event(run_id, "launched", {"job_id": run.job_id})

    def experiment(self, experiment_id: str) -> ExperimentRecord:
        with self.lock:
            if experiment_id not in self.experiments:
                raise UnknownRecordError(experiment_id)
            return self.experiments[experiment_id]

    def list_experiments(self, tenant: str | None = None) -> list[ExperimentRecord]:
        with self.lock:
            return [experiment for experiment in self.experiments.values()
                    if tenant is None or experiment.tenant == tenant]

    def tenant_of_run(self, run_id: str) -> str:
        """Dry runs and runs started elsewhere have no record here; they fall under the default tenant."""
        with self.lock:
            run = self.runs.get(run_id)
        return run.tenant if run is not None else DEFAULT_TENANT

    def run(self, run_id: str) -> RunRecord:
        with self.lock:
            if run_id not in self.runs:
                raise UnknownRecordError(run_id)
            return self.runs[run_id]

    def documents(self) -> list[SpecDocument]:
        """Every launched run's spec, for catalog usage counts."""
        with self.lock:
            return list(self.run_documents.values())

    def run_document(self, run_id: str) -> SpecDocument:
        """The sweep point's full spec, which a trainer job's rollout workers play."""
        with self.lock:
            if run_id not in self.run_documents:
                raise UnknownRecordError(run_id)
            return self.run_documents[run_id]

    def list_runs(self, experiment_id: str | None = None) -> list[RunRecord]:
        with self.lock:
            return [run for run in self.runs.values() if experiment_id is None or run.experiment_id == experiment_id]

    def control(self, run_id: str, command: str, value: Any = None) -> RunRecord:
        """Operational knobs only; a running run's tasks, endpoints and rewards never change."""
        run = self.run(run_id)
        if run.state in TERMINAL_STATES:
            raise ControlError(f"run {run_id} already {run.state}")
        with self.lock:
            if command == "pause":
                run.state = "paused"
            elif command == "resume":
                run.state = "running"
            elif command == "stop":
                run.state = "stopped"
            elif command == "set_concurrency":
                if not isinstance(value, int) or value < 1:
                    raise ControlError("set_concurrency needs a positive integer")
                run.desired_concurrency = value
            elif command == "set_priority":
                if value not in PRIORITIES:
                    raise ControlError(f"priority must be one of {', '.join(PRIORITIES)}")
                run.priority = value
            elif command == "extend_budget":
                if not isinstance(value, (int, float)) or value <= 0:
                    raise ControlError("extend_budget needs a positive amount")
                self.experiments[run.experiment_id].budget_usd += float(value)
            else:
                raise ControlError(
                    f"{command} is not an operational control; changing tasks, endpoints or rewards creates a new run"
                )
        self.reconcile(run, stop_trainer=command == "stop")
        self.record_event(run_id, "control", {"command": command, "value": value})
        return run

    def reconcile(self, run: RunRecord, stop_trainer: bool) -> None:
        """Eval and data runs are pushed to the executor; RL rollout workers poll the run record."""
        if run.run_type != "rl":
            self.executor.apply(run)
        elif stop_trainer and run.job_id:
            self.trainer_launcher.cancel(self.run_documents[run.run_id].run.trainer.launcher, run.job_id)

    def record_spend(self, run_id: str, amount_usd: float) -> ExperimentRecord:
        """Metering reports spend; crossing the budget stops every live run of the experiment."""
        run = self.run(run_id)
        with self.lock:
            experiment = self.experiments[run.experiment_id]
            experiment.spent_usd += amount_usd
            exhausted = experiment.spent_usd >= experiment.budget_usd
        self.record_event(run_id, "spend", {"amount_usd": amount_usd, "experiment_spent_usd": experiment.spent_usd})
        if exhausted:
            for sibling_id in experiment.run_ids:
                if self.run(sibling_id).state not in TERMINAL_STATES:
                    self.control(sibling_id, "stop")
                    self.record_event(sibling_id, "budget_exhausted", {"budget_usd": experiment.budget_usd})
        return experiment

    def mark_finished(self, run_id: str, state: Literal["completed", "failed"], detail: str = "") -> RunRecord:
        run = self.run(run_id)
        with self.lock:
            run.state = state
        self.record_event(run_id, state, {"detail": detail})
        return run

    def publish_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        self.run(run_id)
        self.record_event(run_id, "metrics", metrics)

    def cost_per_thousand_episodes(self) -> dict[str, float]:
        """Each run's spend per 1,000 scored episodes, from its spend events and its latest published metrics."""
        with self.lock:
            events_by_run = {run_id: list(events) for run_id, events in self.events.items()}
        costs = {}
        for run_id, events in events_by_run.items():
            published = [event.detail for event in events if event.kind == "metrics"]
            if not published:
                continue
            latest = published[-1]
            scored = latest.get("episodes", 0) - latest.get("env_errors", 0) - latest.get("human_intervened", 0)
            if scored > 0:
                spent = sum(event.detail.get("amount_usd", 0.0) for event in events if event.kind == "spend")
                costs[run_id] = 1000 * spent / scored
        return costs

    def run_events(self, run_id: str, after_sequence: int = 0) -> list[RunEvent]:
        self.run(run_id)
        with self.lock:
            return [event for event in self.events.get(run_id, []) if event.sequence > after_sequence]

    def sign(self, payload: str) -> str:
        return hmac.new(self.token_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue_run_token(self, run_id: str) -> str:
        payload = f"{run_id}.{int(self.clock() + self.token_ttl_seconds)}"
        return f"{payload}.{self.sign(payload)}"

    def verify_run_token(self, token: str) -> str:
        payload, _, signature = token.rpartition(".")
        run_id, _, expiry_text = payload.rpartition(".")
        if not payload or not hmac.compare_digest(self.sign(payload), signature):
            raise InvalidRunTokenError("run token signature does not match")
        if not expiry_text.isdigit() or int(expiry_text) <= self.clock():
            raise InvalidRunTokenError("run token expired")
        try:
            run = self.run(run_id)
        except UnknownRecordError as error:
            raise InvalidRunTokenError("run token names an unknown run") from error
        if run.state in TERMINAL_STATES:
            raise InvalidRunTokenError(f"run {run_id} already {run.state}")
        return run_id
