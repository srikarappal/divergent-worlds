"""The task authoring kit (DESIGN.md sections 6.10 and 6.14).

The CLI and the console share one flow. An author starts a session from a
saved spec and gets one generated task. Each recording leases a dev env at
interactive priority with the task's setup applied and hands it over through
takeover; ending the recording keeps the author's input as canonical actions
and the final state the verifiers grade. Running the gates replays the oracle
in fresh envs (the latest recorded oracle, or the template's scripted oracle
when none was recorded), adds alternative solutions and near misses, asks a
strong agent when one is configured, then runs every gate. Submitting files
the gate report for human review. With a gate report store, each report and
the recipe behind it become the task's gates for preflight and for reruns when
its image is bumped.
"""

import itertools
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from rlworld.actions.demonstrations import InputEvent, demonstration_actions
from rlworld.actions.models import action_payload, parse_action
from rlworld.rewards.pipeline import JudgeTier
from rlworld.rewards.records import EpisodeOutcome
from rlworld.rollout.episode import TaskInstance
from rlworld.rollout.groups import EnvShape
from rlworld.sdk.client import FleetError
from rlworld.services.authoring_models import AuthoringSession, GateOutcome, Recording, RecordingKind, RecordingStart
from rlworld.services.experiments import ExperimentService, UnknownRecordError
from rlworld.services.gate_reports import GateRecipe, GateRecord, GateReports
from rlworld.services.replays import FreshEnvs, ReplayFleet
from rlworld.services.review import ReviewService
from rlworld.specs.digest import spec_digest
from rlworld.specs.document import SpecDocument
from rlworld.tasks.curriculum import CandidateTask
from rlworld.tasks.gates import GateConfig, GateInputs, GateReport, SolutionAttempt, TaskGateRunner
from rlworld.tasks.generators import TaskGenerationError
from rlworld.tasks.registry import RegistryTaskProvider, SnapshotResolver, TaskRegistry, UnknownSnapshotError

AUTHORING_LEASE_TTL_SECONDS = 3600
AUTHORING_PRIORITY = "interactive"
AUTHORING_ENDPOINT = "authoring"


class AuthoringError(ValueError):
    """A step the session cannot take now: a spec the registry cannot serve, no dev env or a second submission."""


class AuthoringFleet(ReplayFleet, Protocol):
    def start_takeover(self, lease_id: str) -> dict[str, Any]: ...

    def end_takeover(self, lease_id: str) -> dict[str, Any]: ...


class SolvabilityProbe(Protocol):
    """The solvability gate's strong agent: tries attempts at the task, each in a fresh env of the spec's image."""

    def attempts(self, document: SpecDocument, instance: TaskInstance, tries: int) -> list[SolutionAttempt]: ...


@dataclass
class Draft:
    """A session's working state; final states stay here, outside anything the console lists."""

    session: AuthoringSession
    document: SpecDocument
    provider: RegistryTaskProvider
    candidate: CandidateTask
    instance: TaskInstance
    envs: FreshEnvs
    task_index: int
    attempts: dict[str, SolutionAttempt] = field(default_factory=dict)


def gate_outcomes(report: GateReport) -> list[GateOutcome]:
    return [GateOutcome(name=result.name, passed=result.passed, detail=result.detail,
                        attempts=[asdict(attempt) for attempt in result.attempts]) for result in report.results]


def gate_recipe(draft: Draft, recorded: list[Recording]) -> GateRecipe:
    """The solutions a session's gates replayed, kept so an image bump can replay them again."""
    oracles = [recording.actions for recording in recorded if recording.kind == "oracle"]
    return GateRecipe(spec_digest=draft.session.spec_digest, task_index=draft.task_index,
                      oracle=oracles[-1] if oracles else None,
                      alternatives=[recording.actions for recording in recorded if recording.kind == "alternative"],
                      near_misses=[recording.actions for recording in recorded if recording.kind == "near_miss"])


class AuthoringService:
    def __init__(self, experiment_service: ExperimentService, registry: TaskRegistry, snapshots: SnapshotResolver,
                 fleet: AuthoringFleet, review_service: ReviewService, shapes: Mapping[str, EnvShape],
                 judge: JudgeTier | None = None, solvability: SolvabilityProbe | None = None,
                 gate_config: GateConfig | None = None, gate_reports: GateReports | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.experiment_service = experiment_service
        self.registry = registry
        self.snapshots = snapshots
        self.fleet = fleet
        self.review_service = review_service
        self.shapes = dict(shapes)
        self.judge = judge
        self.solvability = solvability
        self.gate_config = gate_config or GateConfig()
        self.gate_reports = gate_reports
        self.clock = clock
        self.drafts: dict[str, Draft] = {}
        self.gate_runs: dict[str, threading.Thread] = {}
        self.session_numbers = itertools.count(1)
        self.recording_numbers = itertools.count(1)
        self.lock = threading.Lock()

    def start(self, spec_digest: str, author: str, tenant: str, task_index: int = 0) -> AuthoringSession:
        document = self.experiment_service.spec(spec_digest)[1]
        vmm = document.image.pool.vmm
        if vmm not in self.shapes:
            raise AuthoringError(f"no env shape is configured for {vmm} pools")
        try:
            provider = RegistryTaskProvider(self.registry, document, self.snapshots)
        except (TaskGenerationError, UnknownSnapshotError) as error:
            raise AuthoringError(error.args[0]) from error
        candidates = provider.candidates()
        if not 0 <= task_index < len(candidates):
            raise AuthoringError(f"task_index must be below {len(candidates)}")
        candidate = candidates[task_index]
        instance = provider.instance(candidate)
        with self.lock:
            session_id = f"authoring_{next(self.session_numbers):06d}"
        session = AuthoringSession(session_id=session_id, spec_digest=spec_digest, author=author, tenant=tenant,
                                   task_id=instance.task_id, template=instance.template,
                                   instruction=instance.instruction, hidden_intent=instance.hidden_intent,
                                   created_at=self.clock())
        envs = FreshEnvs(fleet=self.fleet, shape=self.shapes[vmm], document=document, instance=instance,
                         run_id=session_id, tenant=tenant, priority=AUTHORING_PRIORITY, endpoint=AUTHORING_ENDPOINT,
                         ttl_seconds=AUTHORING_LEASE_TTL_SECONDS)
        with self.lock:
            self.drafts[session_id] = Draft(session, document, provider, candidate, instance, envs, task_index)
        return session.model_copy(deep=True)

    def draft(self, session_id: str) -> Draft:
        with self.lock:
            if session_id not in self.drafts:
                raise UnknownRecordError(session_id)
            return self.drafts[session_id]

    def session(self, session_id: str) -> AuthoringSession:
        draft = self.draft(session_id)
        with self.lock:
            return draft.session.model_copy(deep=True)

    def begin_recording(self, session_id: str, kind: RecordingKind, actor: str) -> RecordingStart:
        draft = self.draft(session_id)
        if draft.session.review_item_id is not None:
            raise AuthoringError(f"{session_id} was already submitted for review")
        try:
            lease = draft.envs.lease()
        except FleetError as error:
            raise AuthoringError(f"no dev env: {error}") from error
        try:
            takeover = self.fleet.start_takeover(lease.lease_id)
        except FleetError as error:
            draft.envs.close_quietly(lease.lease_id)
            raise AuthoringError(f"the dev env cannot be handed over: {error}") from error
        with self.lock:
            recording = Recording(recording_id=f"recording_{next(self.recording_numbers):06d}", kind=kind,
                                  status="recording", recorded_by=actor, lease_id=lease.lease_id)
            draft.session.recordings.append(recording)
        return RecordingStart(recording=recording.model_copy(), takeover_session_id=takeover["session_id"],
                              relay_path=takeover["relay_path"], expires_at=takeover["expires_at"])

    def end_recording(self, session_id: str, recording_id: str) -> Recording:
        """Keeps the author's input and the state they left, then gives the env back."""
        draft = self.draft(session_id)
        with self.lock:
            recording = next((item for item in draft.session.recordings if item.recording_id == recording_id), None)
            if recording is None:
                raise UnknownRecordError(recording_id)
            if recording.status != "recording":
                raise AuthoringError(f"{recording_id} is {recording.status}, not recording")
            recording.status = "saving"
        try:
            record = self.fleet.end_takeover(recording.lease_id)
            actions = demonstration_actions([InputEvent.model_validate(event) for event in record.get("events") or []])
            final_state = draft.envs.final_state(recording.lease_id)
        except (FleetError, ValueError) as error:
            with self.lock:
                recording.status, recording.error = "failed", str(error)
                return recording.model_copy()
        finally:
            draft.envs.close_quietly(recording.lease_id)
        attempt = SolutionAttempt(f"{recording.kind}_{recording_id}", final_state,
                                  EpisodeOutcome(steps_taken=len(actions), final_action="done"))
        with self.lock:
            recording.status, recording.actions = "recorded", [action_payload(action) for action in actions]
            recording.lost_reason = record.get("lost_reason", "")
            recording.complete = not recording.lost_reason
            draft.attempts[recording_id] = attempt
            return recording.model_copy()

    def recorded(self, draft: Draft) -> tuple[list[Recording], dict[str, SolutionAttempt]]:
        with self.lock:
            recorded = [recording.model_copy() for recording in draft.session.recordings
                        if recording.status == "recorded"]
            return recorded, dict(draft.attempts)

    def gate_inputs(self, draft: Draft) -> GateInputs:
        """The latest recorded oracle replayed, or the scripted one; the scripted one also counts as an alternative."""
        recorded, attempts = self.recorded(draft)
        oracles = [recording for recording in recorded if recording.kind == "oracle"]
        scripted = draft.provider.oracle(draft.candidate)
        alternatives = [attempts[recording.recording_id] for recording in recorded if recording.kind == "alternative"]
        if oracles:
            oracle_actions = [parse_action(payload) for payload in oracles[-1].actions]
            alternatives.append(draft.envs.replay(scripted, "scripted_oracle"))
        else:
            oracle_actions = scripted
        oracle_runs = tuple(draft.envs.replay(oracle_actions, f"oracle_run_{index}")
                            for index in range(self.gate_config.rerun_count))
        near_misses = tuple(attempts[recording.recording_id] for recording in recorded if recording.kind == "near_miss")
        agent_attempts = (tuple(self.solvability.attempts(draft.document, draft.instance,
                                                          self.gate_config.solvability_tries))
                          if self.solvability is not None else ())
        return GateInputs(initial_state=draft.instance.initial_state, ground_truth=draft.instance.ground_truth,
                          oracle_runs=oracle_runs, alternative_solutions=tuple(alternatives), near_misses=near_misses,
                          agent_attempts=agent_attempts, instruction=draft.instance.instruction)

    def run_gates(self, session_id: str) -> AuthoringSession:
        draft = self.draft(session_id)
        try:
            runner = TaskGateRunner(self.registry.verifier_registry, self.judge, config=self.gate_config)
            report = runner.run(draft.instance.task, self.gate_inputs(draft))
        except Exception as error:  # a broken verifier or fleet ends this gate run, not the session
            with self.lock:
                draft.session.gate_status, draft.session.gate_error = "error", f"{type(error).__name__}: {error}"
            return self.session(session_id)
        with self.lock:
            draft.session.gate_status = "passed" if report.passed else "failed"
            draft.session.gate_results, draft.session.gate_error = gate_outcomes(report), ""
        self.record_report(draft, report)
        return self.session(session_id)

    def record_report(self, draft: Draft, report: GateReport) -> None:
        """The report becomes the task's gates for preflight; its recipe reruns them when the image is bumped."""
        if self.gate_reports is None:
            return
        document = draft.document
        self.gate_reports.record(GateRecord(
            task_spec_digest=spec_digest(document.task), template=draft.instance.template,
            task_id=draft.instance.task_id, image=document.image.id, image_digest=spec_digest(document.image),
            pool=draft.envs.shape.pool, snapshot=draft.instance.snapshot.digest, report=report,
            recipe=gate_recipe(draft, self.recorded(draft)[0]), ran_at=self.clock(), source="authoring"))

    def start_gates(self, session_id: str) -> AuthoringSession:
        """Gates take minutes of env time, so they run on their own thread while the session reads running."""
        draft = self.draft(session_id)
        with self.lock:
            if draft.session.review_item_id is not None:
                raise AuthoringError(f"{session_id} was already submitted; its report is what reviewers judge")
            if draft.session.gate_status == "running":
                raise AuthoringError(f"gates already run for {session_id}")
            draft.session.gate_status = "running"
            started = draft.session.model_copy(deep=True)
            worker = threading.Thread(target=self.run_gates, args=(session_id,), daemon=True)
            self.gate_runs[session_id] = worker
        worker.start()
        return started

    def submit(self, session_id: str, actor: str) -> AuthoringSession:
        """Files the gate report for human review, passed or failed; failing tasks are what reviewers fix."""
        draft = self.draft(session_id)
        with self.lock:
            session = draft.session
            if session.review_item_id is not None:
                raise AuthoringError(f"{session_id} was already submitted")
            if session.gate_status not in ("passed", "failed"):
                raise AuthoringError("run the gates before submitting; the report is what reviewers judge")
            item = self.review_service.add_item(
                action="task_submission", queue="review", run_id="", episode_ids=[], source="authoring",
                title=f"{session.template} task {session.task_id} {session.gate_status} its gates",
                note=f"authored by {session.author}", created_by=actor,
                payload={"authoring_session": session_id, "spec_digest": session.spec_digest,
                         "task_id": session.task_id, "instruction": session.instruction,
                         "gate_status": session.gate_status,
                         "gate_results": [outcome.model_dump(mode="json") for outcome in session.gate_results],
                         "recordings": [{"recording_id": recording.recording_id, "kind": recording.kind,
                                         "status": recording.status, "actions": len(recording.actions)}
                                        for recording in session.recordings]})
            session.review_item_id = item.item_id
            return session.model_copy(deep=True)
