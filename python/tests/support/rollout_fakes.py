"""Fakes for rollout tests: an in memory fleet, a scripted policy gateway, task builders and a worker stack."""

import copy
import itertools
import random
import re
import threading

from rlworld.actions.adapters import get_adapter
from rlworld.actions.models import Observation
from rlworld.gateway.messages import ChatResponse, TokenTrace, Usage
from rlworld.gateway.metering import CallRecord
from rlworld.rewards import CheckResult, FunctionVerifier, VerifierRegistry
from rlworld.rewards.checks import cells_equal
from rlworld.rollout import (
    EnvShape,
    EpisodeRunner,
    EpisodeSettings,
    GroupRunner,
    LeaseSettings,
    PolicyClient,
    TaskInstance,
)
from rlworld.sdk.client import AdmissionRejectedError, EnvError, LeaseInfo, SnapshotSource, StepResult
from rlworld.services import RewardService
from rlworld.specs import parse_spec_document
from rlworld.tasks import CandidateTask

INITIAL_STATE = {"files": {"reconciliation.xlsx": {"cells": {"B2": {"value": None}}}}}
SOLVED_STATE = {"files": {"reconciliation.xlsx": {"cells": {"B2": {"value": 1250.0}}}}}
GROUND_TRUTH = {"total": 1250.0}
SNAPSHOT = SnapshotSource(digest="sha256:winsnap", state_path="/snapshots/win.state", memory_path="/snapshots/win.ram",
                          base_disk_path="/snapshots/win.qcow2")
SETUP_PAYLOAD = {"files": {"invoices.csv": "invoice,amount\nINV1,1250\n"}, "open_windows": ["Excel"]}
INSTRUCTION = "Enter the reconciled invoice total in cell B2."
STEP_PATTERN = re.compile(r"Step (\d+)\.")
SOLVING_REPLIES = ("Thought: the total is 1250\nAction: type(content='1250')", "Action: finished()")
GIVING_UP_REPLIES = ("Action: finished()",)


def check_total(context):
    cells = context.final_state["files"]["reconciliation.xlsx"]["cells"]
    return [cells_equal("total", cells, {"B2": context.ground_truth["total"]})]


def check_nothing(context):
    return [CheckResult(name="placeholder", tier="semantic", passed=True)]


def verifier_registry() -> VerifierRegistry:
    registry = VerifierRegistry()
    registry.register(FunctionVerifier("verifiers.excel.reconciliation_values_match", "1", "exact", check_total))
    registry.register(FunctionVerifier("verifiers.mail.summary_lists_discrepant_invoices", "1", "semantic",
                                       check_nothing))
    return registry


def rollout_document(example_payload, run_updates=None, task_updates=None):
    """The example spec without its sweep or judge rubric, sized for tests."""
    payload = copy.deepcopy(example_payload)
    payload["experiment"]["sweep"] = {}
    del payload["task"]["judge_rubric"]
    payload["task"]["max_steps"] = 4
    payload["task"].update(task_updates or {})
    payload["run"].update({"group_size": 4, "tasks_per_batch": 2, "batches": 2, "max_concurrent_envs": 8})
    payload["run"].update(run_updates or {})
    if payload["run"]["type"] != "rl":
        del payload["run"]["trainer"]
    return parse_spec_document(payload)


def task_instance(document, task_id="task_000", personas=(), secrets=()) -> TaskInstance:
    return TaskInstance(
        task_id=task_id,
        template=document.task.generator,
        task=document.task,
        task_digest=f"sha256:{task_id}",
        world_digest="sha256:world1842",
        seed=document.world.seed,
        instruction=INSTRUCTION,
        snapshot=SNAPSHOT,
        initial_state=INITIAL_STATE,
        ground_truth=GROUND_TRUTH,
        setup_payload=SETUP_PAYLOAD,
        hidden_intent="Only March invoices count.",
        personas=personas,
        secrets=secrets,
    )


class FakeTaskProvider:
    def __init__(self, document, task_count=3):
        self.document = document
        self.candidate_tasks = [CandidateTask(f"task_{number:03d}", document.task.generator, document.task.difficulty)
                                for number in range(task_count)]
        self.instanced = []
        self.expanded = []

    def candidates(self):
        return self.candidate_tasks

    def instance(self, candidate):
        self.instanced.append(candidate.task_id)
        return task_instance(self.document, candidate.task_id)

    def expand(self, tracker, policy_version):
        self.expanded.append(policy_version)


class FakeFleet:
    """In memory fleetd: typing the right total solves an env and forks copy their source's state."""

    def __init__(self, admission_rejections=0, crashing_leases=(), fork_error=None):
        self.lock = threading.Lock()
        self.lease_numbers = itertools.count(1)
        self.calls = []
        self.solved = {}
        self.frames = {}
        self.open_leases = set()
        self.admission_rejections = admission_rejections
        self.crashing_leases = set(crashing_leases)
        self.fork_error = fork_error

    def record(self, *call):
        with self.lock:
            self.calls.append(call)

    def calls_named(self, name):
        with self.lock:
            return [call for call in self.calls if call[0] == name]

    def open_lease(self, solved=False):
        with self.lock:
            lease_id = f"lease_{next(self.lease_numbers):04d}"
            self.open_leases.add(lease_id)
            self.solved[lease_id] = solved
            self.frames[lease_id] = 0
        return LeaseInfo(lease_id=lease_id, env_id=f"env_{lease_id}", host_id="host_a",
                         expires_at="2026-10-03T12:05:00Z")

    def lease_group(self, request):
        self.record("lease_group", request.group_size, request.priority, request.snapshot.digest)
        with self.lock:
            if self.admission_rejections:
                self.admission_rejections -= 1
                raise AdmissionRejectedError(429, "fleet_full", "no room right now")
        return [self.open_lease()]

    def fork(self, lease_id, count):
        self.record("fork", lease_id, count)
        if self.fork_error is not None:
            raise self.fork_error
        return [self.open_lease(self.solved[lease_id]) for _ in range(count)]

    def inject(self, lease_id, payload):
        self.record("inject", lease_id, payload)

    def heartbeat(self, lease_id):
        self.record("heartbeat", lease_id)
        return LeaseInfo(lease_id=lease_id, env_id=f"env_{lease_id}", host_id="host_a",
                         expires_at="2026-10-03T12:10:00Z")

    def step(self, lease_id, action):
        self.record("step", lease_id, action.name)
        if lease_id in self.crashing_leases:
            raise EnvError(500, "vm_crashed", "the VM crashed")
        with self.lock:
            if action.name == "type" and action.text == "1250":
                self.solved[lease_id] = True
            self.frames[lease_id] += 1
            frame = self.frames[lease_id]
        observation = Observation.model_validate({
            "step_index": frame,
            "screenshot": {"uri": f"s3://frames/{lease_id}_{frame}.webp", "width": 1280, "height": 800,
                           "sha256": f"{lease_id}_{frame}"},
        })
        return StepResult(observation=observation, env_seconds=0.4, settle_seconds=0.3, settle_capped=False)

    def finish(self, lease_id, apps):
        self.record("finish", lease_id, tuple(apps))
        return copy.deepcopy(SOLVED_STATE if self.solved[lease_id] else INITIAL_STATE)

    def checkpoint(self, lease_id):
        return {"state_path": f"/checkpoints/{lease_id}.state"}

    def close(self, lease_id):
        self.record("close", lease_id)
        with self.lock:
            self.open_leases.discard(lease_id)


class ScriptedGateway:
    """Replies by sample index and step; a sample without a script gives up at once."""

    def __init__(self, scripts=None, failure=None, cost_usd=0.01, meter=None):
        self.scripts = scripts if scripts is not None else {0: SOLVING_REPLIES}
        self.failure = failure
        self.cost_usd = cost_usd
        self.meter = meter
        self.requests = []
        self.lock = threading.Lock()

    def complete(self, request):
        with self.lock:
            self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        step_index = int(STEP_PATTERN.search(request.messages[-1]["content"][0]["text"]).group(1))
        replies = self.scripts.get(request.sample_index, GIVING_UP_REPLIES)
        response = ChatResponse(
            text=replies[min(step_index, len(replies) - 1)],
            served_model=request.model,
            usage=Usage(input_tokens=120, output_tokens=6),
            token_trace=TokenTrace(prompt_token_ids=(1, 2, 3, step_index), completion_token_ids=(7, 8),
                                   completion_logprobs=(-0.1, -0.2)),
            latency_seconds=1.5,
            cost_usd=self.cost_usd,
        )
        if self.meter is not None:
            self.meter.record(CallRecord(endpoint=request.endpoint, requested_model=request.model,
                                         served_model=request.model, purpose=request.purpose, run_id=request.run_id,
                                         outcome="ok", cost_usd=self.cost_usd))
        return response


def no_sleep(seconds):
    return None


def build_policy(document, gateway, run_id="run_1"):
    adapter = get_adapter(document.run.policy.action_format)
    geometry = adapter.model_geometry(document.image.display.width, document.image.display.height)
    return PolicyClient(gateway, document.run.policy, adapter, geometry, run_id, document.run.type)


def build_episode_runner(document, fleet, gateway, trajectory_store, run_id="run_1", **options):
    settings = EpisodeSettings(run_id, document.run.type, document.image.os, ("files",), document.run.history)
    finisher = RewardService(fleet, verifier_registry(), random_generator=random.Random(0))
    return EpisodeRunner(fleet, build_policy(document, gateway, run_id), finisher, trajectory_store, settings,
                         **options)


def build_group_runner(document, fleet, gateway, trajectory_store, run_id="run_1", licenses=(), entitlements=None,
                       env_error_retries=1, sleep=no_sleep, **episode_options):
    episode_runner = build_episode_runner(document, fleet, gateway, trajectory_store, run_id,
                                          entitlements=entitlements, **episode_options)
    settings = LeaseSettings(run_id=run_id, tenant="lab", cost_center="research",
                             policy_endpoint=document.run.policy.endpoint, shape=EnvShape("qemu_templated", 4096, 2),
                             licenses=licenses)
    return GroupRunner(fleet, episode_runner, settings, entitlements, env_error_retries, backoff_seconds=2.0,
                       sleep=sleep)
