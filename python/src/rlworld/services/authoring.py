"""The task authoring kit (DESIGN.md sections 6.10 and 6.14).

The CLI and the console share one flow. An author starts a session from a
saved spec and gets one generated task. Each recording leases a dev env at
interactive priority with the task's setup applied and hands it over through
takeover; ending the recording keeps the author's input as canonical actions
and the final state the verifiers grade. Running the gates replays the oracle
in fresh envs (the latest recorded oracle, or the template's scripted oracle
when none was recorded), adds alternative solutions and near misses, asks a
strong agent when one is configured, then runs every gate. Submitting files
the gate report for human review.
"""

import itertools
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from rlworld.actions.demonstrations import InputEvent, demonstration_actions
from rlworld.actions.models import ActionModel, action_payload, parse_action
from rlworld.rewards.pipeline import JudgeTier
from rlworld.rewards.records import EpisodeOutcome
from rlworld.rollout.episode import TaskInstance
from rlworld.rollout.groups import EnvShape
from rlworld.sdk.client import FleetError, LeaseGroupRequest, LeaseInfo, StepResult
from rlworld.services.authoring_models import AuthoringSession, GateOutcome, Recording, RecordingKind, RecordingStart
from rlworld.services.experiments import ExperimentService, UnknownRecordError
from rlworld.services.review import ReviewService
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


class AuthoringFleet(Protocol):
    def lease_group(self, request: LeaseGroupRequest) -> list[LeaseInfo]: ...

    def inject(self, lease_id: str, payload: dict[str, Any]) -> None: ...

    def step(self, lease_id: str, action: ActionModel) -> StepResult: ...

    def finish(self, lease_id: str, apps: list[str]) -> dict[str, Any]: ...

    def close(self, lease_id: str) -> None: ...

    def start_takeover(self, lease_id: str) -> dict[str, Any]: ...

    def end_takeover(self, lease_id: str) -> dict[str, Any]: ...


class SolvabilityProbe(Protocol):
    """The solvability gate's strong agent: tries attempts at the task, each in a fresh env."""

    def attempts(self, instance: TaskInstance, tries: int) -> list[SolutionAttempt]: ...


@dataclass
class Draft:
    """A session's working state; final states stay here, outside anything the console lists."""

    session: AuthoringSession
    document: SpecDocument
    provider: RegistryTaskProvider
    candidate: CandidateTask
    instance: TaskInstance
    shape: EnvShape
    attempts: dict[str, SolutionAttempt] = field(default_factory=dict)


def gate_outcomes(report: GateReport) -> list[GateOutcome]:
    return [GateOutcome(name=result.name, passed=result.passed, detail=result.detail,
                        attempts=[asdict(attempt) for attempt in result.attempts]) for result in report.results]


def playable(actions: Sequence[ActionModel]) -> tuple[list[ActionModel], EpisodeOutcome]:
    """The env actions to replay up to the first ending action with how that ending reads as an outcome."""
    steps: list[ActionModel] = []
    for action in actions:
        if action.ends_episode:
            return steps, EpisodeOutcome(steps_taken=len(steps), final_action=action.name,
                                         answer_text=getattr(action, "text", None))
        if action.modality != "control":
            steps.append(action)
    return steps, EpisodeOutcome(steps_taken=len(steps), final_action="done")


class AuthoringService:
    def __init__(self, experiment_service: ExperimentService, registry: TaskRegistry, snapshots: SnapshotResolver,
                 fleet: AuthoringFleet, review_service: ReviewService, shapes: Mapping[str, EnvShape],
                 judge: JudgeTier | None = None, solvability: SolvabilityProbe | None = None,
                 gate_config: GateConfig | None = None, clock: Callable[[], float] = time.time) -> None:
        self.experiment_service = experiment_service
        self.registry = registry
        self.snapshots = snapshots
        self.fleet = fleet
        self.review_service = review_service
        self.shapes = dict(shapes)
        self.judge = judge
        self.solvability = solvability
        self.gate_config = gate_config or GateConfig()
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
        with self.lock:
            self.drafts[session_id] = Draft(session, document, provider, candidate, instance, self.shapes[vmm])
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

    def lease(self, draft: Draft) -> LeaseInfo:
        """A fresh dev env at the task's start state, leased long enough for an author to work in it."""
        clock = draft.document.world.clock.fixed
        request = LeaseGroupRequest(
            run_id=draft.session.session_id, tenant=draft.session.tenant, pool=draft.shape.pool,
            priority=AUTHORING_PRIORITY, endpoint=AUTHORING_ENDPOINT, snapshot=draft.instance.snapshot,
            memory_mib=draft.shape.memory_mib, vcpus=draft.shape.vcpus, cpu_millicores=draft.shape.cpu_millicores,
            cost_center=AUTHORING_ENDPOINT, ttl_seconds=AUTHORING_LEASE_TTL_SECONDS,
            host_scoped_licenses=draft.shape.host_scoped_licenses,
            world_time=clock.isoformat() if clock is not None else None,
            observation_kinds=tuple(draft.instance.task.observation))
        lease = self.fleet.lease_group(request)[0]
        try:
            self.fleet.inject(lease.lease_id, draft.instance.setup_payload)
        except Exception:
            self.close_quietly(lease.lease_id)
            raise
        return lease

    def close_quietly(self, lease_id: str) -> None:
        try:
            self.fleet.close(lease_id)
        except FleetError:
            return

    def begin_recording(self, session_id: str, kind: RecordingKind, actor: str) -> RecordingStart:
        draft = self.draft(session_id)
        if draft.session.review_item_id is not None:
            raise AuthoringError(f"{session_id} was already submitted for review")
        try:
            lease = self.lease(draft)
        except FleetError as error:
            raise AuthoringError(f"no dev env: {error}") from error
        try:
            takeover = self.fleet.start_takeover(lease.lease_id)
        except FleetError as error:
            self.close_quietly(lease.lease_id)
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
            final_state = self.fleet.finish(recording.lease_id, list(draft.document.world.materialize))
        except (FleetError, ValueError) as error:
            with self.lock:
                recording.status, recording.error = "failed", str(error)
                return recording.model_copy()
        finally:
            self.close_quietly(recording.lease_id)
        attempt = SolutionAttempt(f"{recording.kind}_{recording_id}", final_state,
                                  EpisodeOutcome(steps_taken=len(actions), final_action="done"))
        with self.lock:
            recording.status, recording.actions = "recorded", [action_payload(action) for action in actions]
            recording.lost_reason = record.get("lost_reason", "")
            recording.complete = not recording.lost_reason
            draft.attempts[recording_id] = attempt
            return recording.model_copy()

    def replay(self, draft: Draft, actions: Sequence[ActionModel], name: str) -> SolutionAttempt:
        """Plays a solution in a fresh env; a failing env becomes an env error the gates report."""
        steps, outcome = playable(actions)
        try:
            lease = self.lease(draft)
        except FleetError as error:
            return SolutionAttempt(name, {}, outcome.model_copy(update={"env_error": f"lease failed: {error}"}))
        try:
            for action in steps:
                self.fleet.step(lease.lease_id, action)
            final_state = self.fleet.finish(lease.lease_id, list(draft.document.world.materialize))
        except FleetError as error:
            return SolutionAttempt(name, {}, outcome.model_copy(update={"env_error": f"replay failed: {error}"}))
        finally:
            self.close_quietly(lease.lease_id)
        return SolutionAttempt(name, final_state, outcome)

    def gate_inputs(self, draft: Draft) -> GateInputs:
        """The latest recorded oracle replayed, or the scripted one; the scripted one also counts as an alternative."""
        with self.lock:
            recorded = [recording.model_copy() for recording in draft.session.recordings
                        if recording.status == "recorded"]
            attempts = dict(draft.attempts)
        oracles = [recording for recording in recorded if recording.kind == "oracle"]
        scripted = draft.provider.oracle(draft.candidate)
        alternatives = [attempts[recording.recording_id] for recording in recorded if recording.kind == "alternative"]
        if oracles:
            oracle_actions = [parse_action(payload) for payload in oracles[-1].actions]
            alternatives.append(self.replay(draft, scripted, "scripted_oracle"))
        else:
            oracle_actions = scripted
        oracle_runs = tuple(self.replay(draft, oracle_actions, f"oracle_run_{index}")
                            for index in range(self.gate_config.rerun_count))
        near_misses = tuple(attempts[recording.recording_id] for recording in recorded if recording.kind == "near_miss")
        agent_attempts = (tuple(self.solvability.attempts(draft.instance, self.gate_config.solvability_tries))
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
        return self.session(session_id)

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
