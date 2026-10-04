import copy
import dataclasses
import json

import pytest
from support.experiment_fakes import eval_payload

from rlworld.sdk.client import SnapshotSource
from rlworld.specs import parse_spec_document, set_path, spec_digest
from rlworld.tasks.finance import ReconcileInvoices
from rlworld.tasks.generators import GeneratedTask, TaskGenerationError
from rlworld.tasks.registry import RegistryTaskProvider, StaticSnapshots, TaskRegistry, default_task_registry
from rlworld.tasks.splits import (
    BenchmarkScreen,
    BenchmarkTask,
    HoldoutPolicy,
    OverlapIndex,
    held_out_site,
    holdout_problems,
    load_benchmark,
    shingles,
)
from rlworld.worlds import generate_mid_market_company

WORLD_SIZE = {"employees": 6, "customers": 6, "invoices_per_customer": 3, "emails_per_person": 2}
SNAPSHOT = SnapshotSource(digest="sha256:" + "2b" * 32, state_path="", memory_path="", base_disk_path="")
WEBARENA_INTENT = "What is the top-1 best selling product in 2022"
OSWORLD_INSTRUCTION = "Can you make Bing the main search thingy when I look stuff up on the internet?"


def training_payload(example_payload, **world_network):
    payload = copy.deepcopy(example_payload)
    payload["world"].update({"size": WORLD_SIZE, "materialize": ["mail", "files"]})
    payload["world"]["network"].update(world_network)
    payload["task"].pop("judge_rubric")
    payload["task"]["difficulty"] = {"apps": 2, "oracle_steps": 12, "distractors": 2, "corrupted_inputs": 2,
                                     "dependency_depth": 1}
    payload["run"].update({"tasks_per_batch": 4})
    return payload


POLICY = HoldoutPolicy(apps={"mail", "excel", "crm"}, sites={"crm.example.com"},
                       templates={"tasks.finance.reconcile_invoices"}, world_seeds={1842, 7})


def test_training_specs_may_not_touch_what_eval_holds_out(example_payload):
    document = parse_spec_document(training_payload(example_payload))
    assert holdout_problems(document, POLICY) == [
        "apps excel, mail are held out for eval", "sites crm.example.com are held out for eval",
        "template tasks.finance.reconcile_invoices is held out for eval", "world seed 1842 is held out for eval"]
    assert holdout_problems(document, HoldoutPolicy(world_seeds={7}, apps={"files_app"})) == []
    assert holdout_problems(document, HoldoutPolicy()) == []
    evaluation = parse_spec_document(eval_payload(set_path(training_payload(example_payload), "task.split", "eval")))
    assert holdout_problems(evaluation, POLICY) == []


def test_held_out_sites_cover_their_subdomains():
    assert held_out_site("crm.example.com", ["crm.example.com"])
    assert held_out_site("api.crm.example.com", ["crm.example.com"])
    assert not held_out_site("evilcrm.example.com", ["crm.example.com"])
    assert not held_out_site("example.com", ["crm.example.com"])


def test_allowlisted_and_overridden_hosts_are_both_sites(example_payload):
    payload = training_payload(example_payload)
    payload["world"]["network"]["dns_overrides"] = {"shop.example.com": "shop_clone"}
    document = parse_spec_document(payload)
    assert holdout_problems(document, HoldoutPolicy(sites={"example.com"})) == [
        "sites shop.example.com are held out for eval"]


def write_examples(root):
    for domain, example_id, instruction in (("chrome", "bb5e4c0d", OSWORLD_INSTRUCTION),
                                            ("os", "94d95f96", "I want to install Spotify on my current system.")):
        (root / domain).mkdir(parents=True, exist_ok=True)
        (root / domain / f"{example_id}.json").write_text(json.dumps({"id": example_id, "instruction": instruction}))
    return root


def test_benchmarks_load_from_example_folders_and_task_lists(tmp_path):
    folder = write_examples(tmp_path / "osworld")
    assert load_benchmark("osworld", folder) == [BenchmarkTask("osworld", "bb5e4c0d", OSWORLD_INSTRUCTION),
                                                 BenchmarkTask("osworld", "94d95f96",
                                                               "I want to install Spotify on my current system.")]
    webarena = tmp_path / "test.raw.json"
    webarena.write_text(json.dumps([{"task_id": 0, "sites": ["shopping_admin"], "intent": WEBARENA_INTENT}]))
    assert load_benchmark("webarena", webarena) == [BenchmarkTask("webarena", "0", WEBARENA_INTENT)]
    goals = tmp_path / "workarena.json"
    goals.write_text(json.dumps([{"instruction": "Order an iPad"}, {"id": "w2", "instruction": "Sort the list"}]))
    assert [(task.task_id, task.instruction) for task in load_benchmark("workarena", goals)] == [
        ("0", "Order an iPad"), ("w2", "Sort the list")]


def test_shingles_ignore_case_and_punctuation():
    assert shingles("Open the  Calc,") == frozenset({("open", "the", "calc")})
    assert shingles("Open the calc app now") == frozenset({("open", "the", "calc"), ("the", "calc", "app"),
                                                          ("calc", "app", "now")})
    assert shingles("") == frozenset() and shingles("!!") == frozenset()


def test_near_duplicates_of_benchmark_tasks_are_found(tmp_path):
    tasks = load_benchmark("osworld", write_examples(tmp_path / "osworld"))
    index = OverlapIndex([*tasks, BenchmarkTask("webarena", "0", WEBARENA_INTENT)])
    copied = index.matches("can you make bing the main search thingy when I look stuff up on the internet")
    assert [(match.task.task_id, match.similarity) for match in copied] == [("bb5e4c0d", 1.0)]
    reworded = index.matches("Please make Bing the main search thingy when I look stuff up online")
    assert reworded and reworded[0].task.benchmark == "osworld" and 0.5 <= reworded[0].similarity < 1.0
    assert index.matches("Reconcile the March invoices into the workbook") == []
    strict = OverlapIndex(tasks, threshold=0.95)
    assert strict.matches("Please make Bing the main search thingy when I look stuff up online") == []


def test_the_screen_names_the_closest_benchmark_task(tmp_path):
    screen = BenchmarkScreen(OverlapIndex(load_benchmark("osworld", write_examples(tmp_path / "osworld"))))
    world = generate_world()
    copied = GeneratedTask(instruction=OSWORLD_INSTRUCTION, world=world, ground_truth={})
    assert screen.blocked(copied) == "overlaps osworld task bb5e4c0d at 1.00"
    assert screen.blocked(GeneratedTask(instruction="Total the invoices.", world=world, ground_truth={})) is None


def generate_world():
    return generate_mid_market_company(1, {"employees": 3, "customers": 2})


def snapshots_for(document):
    return StaticSnapshots({spec_digest(document.image): SNAPSHOT})


class InstructionScreen:
    """Blocks the instructions it was given, like a benchmark index holding copies of them."""

    def __init__(self, blocked_instructions):
        self.blocked_instructions = set(blocked_instructions)

    def blocked(self, task):
        return "overlaps a benchmark" if task.instruction in self.blocked_instructions else None


def test_training_providers_skip_blocked_tasks_and_fill_their_place(example_payload):
    document = parse_spec_document(training_payload(example_payload))
    plain = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document))
    candidates = plain.candidates()
    copied = plain.generated_task(candidates[1]).instruction
    screened = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document),
                                    screen=InstructionScreen({copied}))
    assert len(screened.candidates()) == len(candidates)
    assert all(screened.generated_task(candidate).instruction != copied for candidate in screened.candidates())
    blocked_here = {candidate.task_id for candidate in candidates
                    if plain.generated_task(candidate).instruction == copied}
    assert candidates[1].task_id in blocked_here and blocked_here <= set(screened.blocked)
    assert set(screened.blocked.values()) == {"overlaps a benchmark"}
    kept = {candidate.task_id for candidate in screened.candidates()}
    assert kept.isdisjoint(screened.blocked) and kept >= {candidate.task_id for candidate in candidates} - blocked_here


def test_eval_providers_are_never_screened(example_payload):
    document = parse_spec_document(eval_payload(set_path(training_payload(example_payload), "task.split", "eval")))
    provider = RegistryTaskProvider(default_task_registry(), document, snapshots_for(document),
                                    screen=InstructionScreen(set()))
    assert provider.screen is None and provider.blocked == {}


class FixedInstruction(ReconcileInvoices):
    """Every task asks the same thing, so one benchmark copy blocks them all."""

    def generate(self, world, difficulty, seed):
        return dataclasses.replace(super().generate(world, difficulty, seed), instruction=OSWORLD_INSTRUCTION)


def test_a_run_whose_every_task_is_blocked_does_not_start(example_payload):
    registry = TaskRegistry()
    registry.register(FixedInstruction())
    document = parse_spec_document(training_payload(example_payload))
    with pytest.raises(TaskGenerationError, match="every task this run would train on is blocked: overlaps"):
        RegistryTaskProvider(registry, document, snapshots_for(document),
                             max_tasks_per_difficulty=6, screen=InstructionScreen({OSWORLD_INSTRUCTION}))
