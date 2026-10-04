import copy

import pytest
from support.experiment_fakes import eval_payload

from rlworld.actions.models import DoneAction
from rlworld.rewards.checks import check_result
from rlworld.rewards.verifiers import FunctionVerifier
from rlworld.sdk.client import SnapshotSource
from rlworld.specs import parse_spec_document, spec_digest
from rlworld.specs.digest import content_digest
from rlworld.tasks.finance import ReconcileInvoices
from rlworld.tasks.generators import GeneratedTask, TaskGenerationError, TaskSuite, generated_task_digest
from rlworld.tasks.registry import (
    RegistryTaskProvider,
    StaticSnapshots,
    TaskRegistry,
    default_task_registry,
    setup_payload,
)
from rlworld.worlds import generate_mid_market_company
from rlworld.worlds.model import world_digest

SUITE_NAME = "tasks.public.letters"
SUITE_ORACLE = "oracles.public.letters"
SUITE_VERIFIER = "verifiers.public.letters"
LETTERS = "abc"
SNAPSHOT = SnapshotSource(digest="sha256:" + "5e" * 32, state_path="", memory_path="", base_disk_path="")
WORLD_SNAPSHOT = SnapshotSource(digest="sha256:" + "6f" * 32, state_path="", memory_path="", base_disk_path="")


def letter_written(context):
    letter = context.ground_truth["letter"]
    return [check_result("letter", "exact", letter in str(context.final_state.get("reads")), f"no {letter}")]


class LetterSuite:
    """A fixed suite of three tasks; task i writes the i-th letter and reads it back."""

    name = SUITE_NAME
    version = "1"
    oracle_name = SUITE_ORACLE
    description = "Three fixed tasks."
    world_generators = frozenset({"worlds.mid_market_company"})
    size = len(LETTERS)

    def generate(self, world, difficulty, seed):
        if not 0 <= seed < self.size:
            raise TaskGenerationError(f"no task {seed}")
        letter = LETTERS[seed]
        return GeneratedTask(instruction=f"Write {letter}", world=world, ground_truth={"letter": letter},
                             setup=({"kind": "file", "path": f"{letter}.txt", "text": letter},),
                             reads={"files": [f"{letter}.txt"]})

    def oracle(self, task):
        return [DoneAction()]

    def verifiers(self):
        return (FunctionVerifier(SUITE_VERIFIER, self.version, "exact", letter_written),)


def suite_payload(example_payload, tasks_per_batch=5, batches=1, rl=False):
    payload = copy.deepcopy(example_payload) if rl else eval_payload(example_payload)
    payload["world"].update({"size": {"employees": 3, "customers": 2}, "materialize": []})
    payload["task"].update({"generator": SUITE_NAME, "oracle": SUITE_ORACLE,
                            "verifiers": {"exact": [SUITE_VERIFIER], "semantic": []}})
    payload["task"].pop("judge_rubric")
    payload["run"].update({"tasks_per_batch": tasks_per_batch, "batches": batches})
    return payload


def suite_registry():
    registry = TaskRegistry()
    registry.register(LetterSuite())
    return registry


def snapshots_for(document):
    return StaticSnapshots({spec_digest(document.image): SNAPSHOT})


def provider_for(payload, worlds=None):
    document = parse_spec_document(payload)
    return RegistryTaskProvider(suite_registry(), document, snapshots_for(document), worlds=worlds)


def instructions(provider):
    return [provider.instance(candidate).instruction for candidate in provider.candidates()]


def test_suites_are_told_apart_from_generators():
    assert isinstance(LetterSuite(), TaskSuite)
    assert not isinstance(ReconcileInvoices(), TaskSuite)


def test_a_suite_hands_out_its_tasks_in_order_and_stops_at_its_size(example_payload):
    assert instructions(provider_for(suite_payload(example_payload, tasks_per_batch=5))) == [
        "Write a", "Write b", "Write c"]
    assert instructions(provider_for(suite_payload(example_payload, tasks_per_batch=2))) == ["Write a", "Write b"]
    provider = provider_for(suite_payload(example_payload, tasks_per_batch=2, batches=2))
    assert [provider.tasks[candidate.task_id].seed for candidate in provider.candidates()] == [0, 1, 2]
    provider.add_tasks(provider.candidates()[0].difficulty, 5)
    assert len(provider.candidates()) == 3


def test_suite_task_ids_are_stable_across_providers(example_payload):
    first = provider_for(suite_payload(example_payload)).candidates()
    second = provider_for(suite_payload(example_payload)).candidates()
    assert [candidate.task_id for candidate in first] == [candidate.task_id for candidate in second]
    assert len({candidate.task_id for candidate in first}) == 3


def test_suites_run_only_as_eval_on_the_eval_split(example_payload):
    rl_document = parse_spec_document(suite_payload(example_payload, rl=True))
    assert any("runs only as eval" in problem for problem in suite_registry().problems(rl_document))
    with pytest.raises(TaskGenerationError, match="runs only as eval"):
        RegistryTaskProvider(suite_registry(), rl_document, snapshots_for(rl_document))
    holdout = suite_payload(example_payload)
    holdout["task"]["split"] = "holdout"
    assert any("eval split" in problem for problem in suite_registry().problems(parse_spec_document(holdout)))
    assert suite_registry().problems(parse_spec_document(suite_payload(example_payload))) == []


class EmptySuite(LetterSuite):
    size = 0


def test_an_empty_suite_is_a_problem_rather_than_a_run_with_no_tasks(example_payload):
    registry = TaskRegistry()
    registry.register(EmptySuite())
    document = parse_spec_document(suite_payload(example_payload))
    assert registry.problems(document) == [f"{SUITE_NAME} has no tasks"]
    with pytest.raises(TaskGenerationError, match="has no tasks"):
        RegistryTaskProvider(registry, document, snapshots_for(document))


def test_setup_steps_and_read_plans_travel_in_the_setup_payload(example_payload):
    provider = provider_for(suite_payload(example_payload))
    instance = provider.instance(provider.candidates()[1])
    assert instance.setup_payload["setup"] == [{"kind": "file", "path": "b.txt", "text": "b"}]
    assert instance.setup_payload["reads"] == {"files": ["b.txt"]}
    assert instance.setup_payload["apps"] == [] and instance.initial_state == {}
    plain = GeneratedTask(instruction="x", world=generate_mid_market_company(1, {"employees": 2}), ground_truth={})
    assert set(setup_payload(plain, ("files",))) == {"world", "apps"}


class PreparedWorlds:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def prepare(self, base, world, apps, image):
        return self.snapshot


def test_world_snapshots_keep_the_setup_steps_and_read_plan(example_payload):
    provider = provider_for(suite_payload(example_payload), worlds=PreparedWorlds(WORLD_SNAPSHOT))
    instance = provider.instance(provider.candidates()[0])
    assert instance.snapshot == WORLD_SNAPSHOT
    assert instance.setup_payload["setup"][0]["path"] == "a.txt" and instance.setup_payload["reads"]


def test_task_digests_cover_setup_and_reads_only_when_present():
    template = ReconcileInvoices()
    world = generate_mid_market_company(1, {"employees": 3, "customers": 2})
    plain = GeneratedTask(instruction="Total the invoices", world=world, ground_truth={"total": 1})
    earlier_material = {"template": template.name, "version": template.version, "instruction": plain.instruction,
                        "world": world_digest(world), "ground_truth": plain.ground_truth, "hidden_intent": "",
                        "open_windows": []}
    assert generated_task_digest(template, plain) == content_digest(earlier_material)
    with_setup = GeneratedTask(instruction=plain.instruction, world=world, ground_truth=plain.ground_truth,
                               setup=({"kind": "sleep", "seconds": 1.0},))
    with_reads = GeneratedTask(instruction=plain.instruction, world=world, ground_truth=plain.ground_truth,
                               reads={"files": ["out.txt"]})
    digests = {generated_task_digest(template, task) for task in (plain, with_setup, with_reads)}
    assert len(digests) == 3


def test_the_default_registry_has_no_suite():
    assert not any(isinstance(template, TaskSuite) for template in default_task_registry().templates.values())
