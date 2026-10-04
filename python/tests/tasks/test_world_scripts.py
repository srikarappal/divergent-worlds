import copy
from dataclasses import replace

import pytest
from support.experiment_fakes import eval_payload

from rlworld.rollout import NonPlayer, ScheduledDelivery
from rlworld.sdk.client import SnapshotSource
from rlworld.specs import parse_spec_document, spec_digest
from rlworld.specs.models import DifficultySpec
from rlworld.tasks.finance import ReconcileInvoices
from rlworld.tasks.generators import NpcSpec, TaskGenerationError, generated_task_digest
from rlworld.tasks.registry import RegistryTaskProvider, StaticSnapshots, TaskRegistry, non_players, scheduled_events
from rlworld.worlds import event_record, generate_mid_market_company

WORLD_SIZE = {"employees": 6, "customers": 4, "invoices_per_customer": 2, "emails_per_person": 1}
SNAPSHOT = SnapshotSource(digest="sha256:" + "4d" * 32, state_path="", memory_path="", base_disk_path="")
KNOWLEDGE = "INV-1007 is disputed and should stay out of the open total."


class ColleagueReconcile(ReconcileInvoices):
    """The reference template with the CFO answering questions in chat."""

    def __init__(self, person_id="person_002"):
        self.person_id = person_id

    def generate(self, world, difficulty, seed):
        return replace(super().generate(world, difficulty, seed), npcs=(NpcSpec(self.person_id, KNOWLEDGE),))


def reconcile_payload(example_payload, apps):
    payload = eval_payload(copy.deepcopy(example_payload))
    payload["world"].update({"size": WORLD_SIZE, "materialize": list(apps)})
    payload["task"].pop("judge_rubric")
    payload["task"]["difficulty"] = {"apps": 1, "oracle_steps": 10}
    payload["run"].update({"tasks_per_batch": 1, "batches": 1})
    return parse_spec_document(payload)


def first_instance(document, template=None):
    registry = TaskRegistry()
    registry.register(template or ReconcileInvoices())
    provider = RegistryTaskProvider(registry, document, StaticSnapshots({spec_digest(document.image): SNAPSHOT}))
    return provider.instance(provider.candidates()[0]), provider


def test_instances_carry_the_worlds_events_for_the_runs_apps(example_payload):
    instance, provider = first_instance(reconcile_payload(example_payload, ("mail", "files", "chat")))
    event = provider.world.events[0]
    assert instance.world_events == (ScheduledDelivery(event.trigger_step, "mail", event_record(event, provider.world),
                                                       f"world event {event.id}"),)
    assert instance.world_events[0].step == 10
    files_only, _ = first_instance(reconcile_payload(example_payload, ("files",)))
    assert files_only.world_events == ()


def test_scheduled_events_skip_apps_the_run_does_not_materialize():
    world = generate_mid_market_company(2, WORLD_SIZE)
    assert [delivery.label for delivery in scheduled_events(world, ("mail",))] == ["world event event_cfo_nudge"]
    assert scheduled_events(world, ("chat", "files")) == ()


def test_colleagues_are_played_as_who_they_are_in_the_world(example_payload):
    instance, provider = first_instance(reconcile_payload(example_payload, ("mail", "files", "chat")),
                                        ColleagueReconcile())
    person = provider.world.person("person_002")
    assert person.id != provider.world.agent_persona_id
    assert instance.npcs == (NonPlayer(
        person_id="person_002", handle=person.email.split("@")[0],
        persona=f"{person.full_name}, Chief financial officer in Finance at {provider.world.organization_name}",
        knowledge=KNOWLEDGE),)
    plain, _ = first_instance(reconcile_payload(example_payload, ("mail", "files", "chat")))
    assert plain.npcs == ()
    assert instance.task_digest != plain.task_digest


@pytest.mark.parametrize("person_id", ["person_404", "person_agent"])
def test_a_colleague_must_be_someone_else_in_the_world(example_payload, person_id):
    document = reconcile_payload(example_payload, ("mail", "files", "chat"))
    world = generate_mid_market_company(document.world.seed, WORLD_SIZE)
    chosen = world.agent_persona_id if person_id == "person_agent" else person_id
    with pytest.raises(TaskGenerationError, match=f"colleague {chosen} is nobody else in the world"):
        first_instance(document, ColleagueReconcile(chosen))
    with pytest.raises(TaskGenerationError):
        non_players(world, (NpcSpec(chosen, KNOWLEDGE),))


def test_colleagues_change_the_task_digest_only_when_present():
    template = ReconcileInvoices()
    world = generate_mid_market_company(4, WORLD_SIZE)
    task = template.generate(world, DifficultySpec(apps=1, oracle_steps=10), 7)
    with_colleague = replace(task, npcs=(NpcSpec("person_002", KNOWLEDGE),))
    other_knowledge = replace(task, npcs=(NpcSpec("person_002", "Something else."),))
    digests = {generated_task_digest(template, item) for item in (task, with_colleague, other_knowledge)}
    assert len(digests) == 3
