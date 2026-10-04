import copy
import csv
import io
import os
import subprocess
import sys
import threading
from pathlib import Path

import openpyxl
import pytest
from support.experiment_fakes import eval_payload

from rlworld.actions.models import DoneAction, ExecAction
from rlworld.rewards.records import EpisodeOutcome
from rlworld.rewards.verifiers import VerifierContext
from rlworld.rollout.groups import EnvShape
from rlworld.sdk.client import SnapshotNotFoundError, SnapshotSource
from rlworld.specs import load_spec_document, parse_spec_document, spec_digest
from rlworld.specs.models import DifficultySpec
from rlworld.tasks import GateInputs, PassRateTracker, SolutionAttempt, TaskGateRunner
from rlworld.tasks.curriculum import difficulty_key
from rlworld.tasks.finance import SUMMARY_VERIFIER, ReconcileInvoices, oracle_script
from rlworld.tasks.generators import GeneratedTask, TaskGenerationError
from rlworld.tasks.registry import (
    FleetSnapshots,
    FleetWorlds,
    RegistryTaskProvider,
    StaticSnapshots,
    UnknownSnapshotError,
    default_task_registry,
    start_state,
)
from rlworld.worlds import extract_world_state, generate_mid_market_company, materialize_world
from rlworld.worlds.integrations import USER_WORLD_FOLDER
from rlworld.worlds.model import World, world_digest
from rlworld.worlds.perturbations import INVOICE_EXPORT_PATH

WORLD_SIZE = {"employees": 6, "customers": 6, "invoices_per_customer": 3, "emails_per_person": 2}
APPS = ("mail", "files")
DONE = EpisodeOutcome(steps_taken=2, final_action="done")
SNAPSHOT = SnapshotSource(digest="sha256:dev", state_path="", memory_path="", base_disk_path="rlworld-dev/noble:1")


def difficulty(**changes):
    return DifficultySpec(**{"apps": 2, "oracle_steps": 12, "distractors": 2, "corrupted_inputs": 2,
                             "dependency_depth": 1, **changes})


def base_world():
    return generate_mid_market_company(11, WORLD_SIZE)


def export_amounts(world):
    export = next(document for document in world.documents if document.path == INVOICE_EXPORT_PATH)
    return {row["invoice_id"]: row["amount"] for row in csv.DictReader(io.StringIO(export.content))}


def reconcile_payload(example_payload, rl=False, **task_changes):
    payload = copy.deepcopy(example_payload) if rl else eval_payload(example_payload)
    payload["world"].update({"size": WORLD_SIZE, "materialize": list(APPS)})
    payload["task"].update({"difficulty": difficulty().model_dump(mode="json"), "max_steps": 30, **task_changes})
    payload["task"].pop("judge_rubric")
    payload["run"].update({"tasks_per_batch": 3, "batches": 2})
    return payload


def snapshots_for(document):
    return StaticSnapshots({spec_digest(document.image): SNAPSHOT})


def run_oracle(task, home):
    """What the guest does with the oracle: materialize the task world, then run the script as the user."""
    world_root = home / USER_WORLD_FOLDER
    materialize_world(task.world, APPS, world_root)
    subprocess.run([sys.executable, "-c", oracle_script(task)], env={**os.environ, "HOME": str(home)}, check=True)
    return extract_world_state(APPS, world_root)


def edited_workbook(home, cells):
    path = home / USER_WORLD_FOLDER / "files" / "reconciliation.xlsx"
    workbook = openpyxl.load_workbook(path)
    for cell_name, value in cells.items():
        workbook.active[cell_name] = value
    workbook.save(path)
    return extract_world_state(APPS, home / USER_WORLD_FOLDER)


def test_tasks_are_deterministic_and_follow_the_axes():
    template, world = ReconcileInvoices(), base_world()
    task = template.generate(world, difficulty(), seed=5)
    assert task == template.generate(world, difficulty(), seed=5)
    assert task != template.generate(world, difficulty(), seed=6)
    truth = task.ground_truth
    in_month = [invoice for invoice in world.invoices if invoice.issued_on.strftime("%Y-%m") == truth["month"]]
    assert truth["totals"]["open"] == round(sum(invoice.amount for invoice in in_month if invoice.status == "open"), 2)
    assert truth["cells"] == {"A1": "open", "B1": truth["totals"]["open"], "A2": "overdue",
                              "B2": truth["totals"]["overdue"]}
    original_amounts = export_amounts(world)
    damaged = {invoice_id for invoice_id, amount in export_amounts(task.world).items()
               if amount != original_amounts[invoice_id]}
    assert damaged == set(truth["discrepant_invoice_ids"]) and len(damaged) == 2
    assert {invoice.id for invoice in in_month} >= damaged
    assert len(task.world.emails) == len(world.emails) + 2
    assert truth["summary_recipient"] == next(person.email for person in world.people if person.id == "person_002")
    assert truth["summary_states_totals"] and task.hidden_intent == ""
    assert "issued in" in task.instruction and truth["summary_recipient"] in task.instruction


def test_ambiguity_moves_details_into_the_hidden_intent():
    template, world = ReconcileInvoices(), base_world()
    low = template.generate(world, difficulty(ambiguity="low"), seed=5)
    assert "issued in" not in low.instruction and low.hidden_intent.startswith("Only invoices issued in")
    high = template.generate(world, difficulty(ambiguity="high"), seed=5)
    assert "finance leadership" in high.instruction
    assert high.ground_truth["summary_recipient"] in high.hidden_intent
    workbook_only = template.generate(world, difficulty(apps=1, dependency_depth=0), seed=5)
    assert workbook_only.ground_truth["summary_recipient"] is None and "email" not in workbook_only.instruction
    with pytest.raises(TaskGenerationError, match="at most 2 apps"):
        template.generate(world, difficulty(apps=3), seed=5)


def test_the_scripted_oracle_passes_every_gate(example_payload, tmp_path):
    registry = default_task_registry()
    document = parse_spec_document(reconcile_payload(example_payload))
    task = ReconcileInvoices().generate(base_world(), difficulty(), seed=5)
    oracle = ReconcileInvoices().oracle(task)
    assert isinstance(oracle[0], ExecAction) and oracle[0].command.startswith("/opt/rlworld/bin/python - <<")
    assert isinstance(oracle[-1], DoneAction)
    initial_state = start_state(task.world, APPS)
    home = tmp_path / "home"
    oracle_state = run_oracle(task, home)
    truth = task.ground_truth
    formatted = edited_workbook(home, {"A1": "Open ", "B1": f"{truth['totals']['open']:,.2f}"})
    swapped = edited_workbook(home, {"B1": truth["totals"]["overdue"], "B2": truth["totals"]["open"]})
    report = TaskGateRunner(registry.verifier_registry).run(document.task, GateInputs(
        initial_state=initial_state, ground_truth=truth, instruction=task.instruction,
        oracle_runs=tuple(SolutionAttempt(f"oracle_{index}", oracle_state, DONE) for index in range(3)),
        alternative_solutions=(SolutionAttempt("formatted_amounts", formatted, DONE),),
        near_misses=(SolutionAttempt("swapped_totals", swapped, DONE),),
        agent_attempts=(SolutionAttempt("strong_agent", oracle_state, DONE),)))
    assert report.passed, report.review_reasons


def test_the_summary_must_name_every_damaged_row(example_payload, tmp_path):
    registry = default_task_registry()
    document = parse_spec_document(reconcile_payload(example_payload))
    task = ReconcileInvoices().generate(base_world(), difficulty(), seed=5)
    final_state = run_oracle(task, tmp_path)
    summary = final_state["mail"]["sent"][-1]
    summary["body"] = summary["body"].replace(task.ground_truth["discrepant_invoice_ids"][0], "an invoice")
    verifier = registry.verifier_registry.resolve([SUMMARY_VERIFIER], "semantic")[0]
    context = VerifierContext(task=document.task, initial_state=start_state(task.world, APPS),
                              final_state=final_state, changes=(), ground_truth=task.ground_truth)
    assert [(result.name, result.passed) for result in verifier.verify(context)] == [
        ("summary_lists_discrepant_invoices", False), ("summary_states_totals", True)]
    untouched = VerifierContext(task=document.task, initial_state=context.initial_state,
                                final_state=context.initial_state, changes=(), ground_truth=task.ground_truth)
    assert [result.name for result in verifier.verify(untouched)] == ["summary_sent"]


def test_the_registry_checks_task_specs_against_its_templates(example_payload):
    registry = default_task_registry()
    assert registry.problems(parse_spec_document(reconcile_payload(example_payload))) == []
    wrong = reconcile_payload(example_payload, oracle="oracles.finance.other",
                              verifiers={"exact": ["verifiers.mail.summary_lists_discrepant_invoices"]})
    problems = registry.problems(parse_spec_document(wrong))
    assert any("oracle oracles.finance.other" in problem for problem in problems)
    assert any(problem.startswith("exact verifiers:") for problem in problems)
    unknown = reconcile_payload(example_payload, generator="tasks.hr.onboard_hire")
    assert registry.problems(parse_spec_document(unknown)) == ["task generator tasks.hr.onboard_hire is not registered"]


def test_an_eval_suite_is_stable_and_instances_carry_setup_and_start_state(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload))
    provider = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document))
    candidates = provider.candidates()
    assert len(candidates) == 6 and len({candidate.task_id for candidate in candidates}) == 6
    again = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document))
    assert [candidate.task_id for candidate in again.candidates()] == [candidate.task_id for candidate in candidates]
    instance = provider.instance(candidates[1])
    assert instance.snapshot == SNAPSHOT and instance.template == "tasks.finance.reconcile_invoices"
    assert instance.setup_payload["apps"] == ["mail", "files"]
    assert instance.initial_state["files"]["invoices.csv"]["kind"] == "csv"
    assert instance.initial_state == start_state(World.model_validate(instance.setup_payload["world"]), APPS)
    assert instance.task.difficulty == candidates[1].difficulty and instance.task_digest.startswith("sha256:")
    assert instance.task_digest == again.instance(again.candidates()[1]).task_digest


def test_instances_are_generated_once_across_threads(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload))
    provider = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document))
    candidate = provider.candidates()[2]
    results = []
    threads = [threading.Thread(target=collect_instance, args=(provider, candidate, results)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(result is results[0] for result in results) and len(results) == 4


def collect_instance(provider, candidate, results):
    results.append(provider.instance(candidate))


def test_the_provider_keeps_each_generated_task_and_its_scripted_oracle(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload))
    provider = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document))
    candidate = provider.candidates()[1]
    generated = provider.generated_task(candidate)
    assert provider.generated_task(candidate) is generated
    assert provider.oracle(candidate) == ReconcileInvoices().oracle(generated)
    assert provider.instance(candidate).instruction == generated.instruction
    assert provider.oracle(provider.candidates()[2]) != provider.oracle(candidate)


def test_rl_pools_grow_toward_the_curriculum_frontier(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload, rl=True, user_simulator=None,
                                                     difficulty=difficulty(ambiguity="none").model_dump(mode="json")))
    provider = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document))
    assert len(provider.candidates()) == 3
    tracker = PassRateTracker(prior_successes=0.0, prior_failures=0.0)
    base_bucket = difficulty_key(document.task.difficulty)
    tracker.record("tasks.finance.reconcile_invoices", base_bucket, 0, successes=5, attempts=10)
    provider.expand(tracker, policy_version=0)
    frontier = [candidate for candidate in provider.candidates() if candidate.difficulty == document.task.difficulty]
    assert len(frontier) == 3 + 16
    too_easy = PassRateTracker(prior_successes=0.0, prior_failures=0.0)
    too_easy.record("tasks.finance.reconcile_invoices", base_bucket, 0, successes=10, attempts=10)
    provider.expand(too_easy, policy_version=0)
    harder = [candidate for candidate in provider.candidates() if candidate.difficulty != document.task.difficulty]
    assert harder and all(candidate.difficulty.distractors > 2 for candidate in harder)
    provider.instance(harder[0])


def test_runs_need_a_known_snapshot_and_supported_apps(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload))
    with pytest.raises(UnknownSnapshotError, match=document.image.id):
        RegistryTaskProvider(default_task_registry(), document, StaticSnapshots({}))
    everything = parse_spec_document(example_payload)
    with pytest.raises(TaskGenerationError, match="at most 2 apps"):
        RegistryTaskProvider(default_task_registry(), everything, snapshots_for(everything))


def test_the_linux_example_runs_the_reference_task():
    example = Path(__file__).resolve().parents[3] / "examples" / "specs" / "reconcile_invoices_linux.yaml"
    document = load_spec_document(example.read_text())
    registry = default_task_registry()
    assert registry.problems(document) == []
    provider = RegistryTaskProvider(registry, document, snapshots_for(document))
    instance = provider.instance(provider.candidates()[0])
    assert len(provider.candidates()) == 4 and instance.ground_truth["summary_states_totals"]
    assert "Calc" not in instance.setup_payload.get("open_windows", [])


class SnapshotIndexStub:
    """fleetd's snapshot index: the current snapshot per image digest and pool."""

    def __init__(self, currents):
        self.currents = currents
        self.asked = []

    def current_snapshot(self, image_digest, pool):
        self.asked.append((image_digest, pool))
        if (image_digest, pool) not in self.currents:
            raise SnapshotNotFoundError(404, "unknown_snapshot", "no current snapshot")
        return {"image_digest": image_digest, "pool": pool, "snapshot": self.currents[(image_digest, pool)]}


def test_fleet_snapshots_name_the_current_snapshot_for_hosts_to_pull(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload))
    vmm = document.image.pool.vmm
    index = SnapshotIndexStub({(spec_digest(document.image), "windows"): "sha256:current"})
    resolver = FleetSnapshots(index, {vmm: "windows"})
    source = resolver.snapshot(document.image)
    assert source == SnapshotSource(digest="sha256:current", state_path="", memory_path="", base_disk_path="")
    provider = RegistryTaskProvider(default_task_registry(), document, resolver)
    assert provider.instance(provider.candidates()[0]).snapshot == source
    with pytest.raises(UnknownSnapshotError, match=f"no pool runs {vmm} images"):
        FleetSnapshots(index, {}).snapshot(document.image)
    with pytest.raises(UnknownSnapshotError, match=f"no snapshot of image {document.image.id} on linux yet"):
        FleetSnapshots(index, {vmm: "linux"}).snapshot(document.image)


WORLD_SNAPSHOT = SnapshotSource(digest="sha256:" + "3c" * 32, state_path="", memory_path="", base_disk_path="")


class PreparedWorlds:
    """A world preparer that already decided: a world snapshot, or none."""

    def __init__(self, snapshot):
        self.decided = snapshot
        self.calls = []

    def prepare(self, base, world, apps, image):
        self.calls.append((base, world_digest(world), apps, image.id))
        return self.decided


class WorldIndexStub:
    def __init__(self, decision):
        self.decision = decision
        self.asked = []

    def prepare_world(self, base_digest, world_digest, payload, apps, pool, memory_mib, cpu_millicores, vcpus):
        self.asked.append((base_digest, world_digest, payload, apps, pool, memory_mib, cpu_millicores, vcpus))
        return self.decision


def test_instances_restore_from_a_world_snapshot_and_inject_only_their_changes(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload))
    worlds = PreparedWorlds(WORLD_SNAPSHOT)
    provider = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document), worlds=worlds)
    plain = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document))
    assert worlds.calls == [(SNAPSHOT, provider.world_digest, APPS, document.image.id)]
    instance, full = provider.instance(provider.candidates()[0]), plain.instance(plain.candidates()[0])
    assert instance.snapshot == WORLD_SNAPSHOT and full.snapshot == SNAPSHOT
    injected = World.model_validate(instance.setup_payload["world"])
    assert len(injected.emails) == 2 and [document.path for document in injected.documents] == ["invoices.csv"]
    assert len(World.model_validate(full.setup_payload["world"]).emails) > len(injected.emails)
    assert (instance.initial_state, instance.task_digest, instance.ground_truth) == (
        full.initial_state, full.task_digest, full.ground_truth)
    undecided = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document),
                                     worlds=PreparedWorlds(None))
    assert undecided.instance(undecided.candidates()[0]).snapshot == SNAPSHOT


def test_a_task_that_removes_part_of_its_world_restores_from_the_base(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload))
    provider = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document),
                                    worlds=PreparedWorlds(WORLD_SNAPSHOT))
    generated = provider.generated_task(provider.candidates()[0])
    trimmed = generated.world.model_copy(update={"emails": generated.world.emails[1:]})
    snapshot, setup = provider.start_from(GeneratedTask(instruction=generated.instruction, world=trimmed,
                                                        ground_truth=generated.ground_truth))
    assert snapshot == SNAPSHOT and setup["world"] == trimmed.model_dump(mode="json")


def test_fleet_worlds_ask_fleetd_with_the_runs_pool_shape(example_payload):
    document = parse_spec_document(reconcile_payload(example_payload))
    shape = EnvShape(pool="windows", memory_mib=6144, vcpus=2, cpu_millicores=3000)
    index = WorldIndexStub({"snapshot": WORLD_SNAPSHOT.digest, "deterministic": True})
    world = base_world()
    prepared = FleetWorlds(index, {document.image.pool.vmm: shape}).prepare(SNAPSHOT, world, APPS, document.image)
    assert prepared == WORLD_SNAPSHOT
    base_digest, digest, payload, apps, pool, memory, cpu, vcpus = index.asked[0]
    assert (base_digest, digest, apps, pool, memory, cpu, vcpus) == (SNAPSHOT.digest, world_digest(world),
                                                                     list(APPS), "windows", 6144, 3000, 2)
    assert payload == {"world": world.model_dump(mode="json"), "apps": list(APPS)}
    fast = FleetWorlds(WorldIndexStub({"snapshot": None, "deterministic": True}), {document.image.pool.vmm: shape})
    assert fast.prepare(SNAPSHOT, world, APPS, document.image) is None
