from rlworld.specs.models import DifficultySpec
from rlworld.tasks.finance import ReconcileInvoices
from rlworld.worlds import extract_world_state, generate_mid_market_company, materialize_world
from rlworld.worlds.delta import world_delta
from rlworld.worlds.model import World

SIZE = {"employees": 6, "customers": 6, "invoices_per_customer": 3, "emails_per_person": 2}
APPS = ("mail", "files")


def task_world(world):
    difficulty = DifficultySpec(apps=2, oracle_steps=12, distractors=3, corrupted_inputs=2, dependency_depth=1)
    return ReconcileInvoices().generate(world, difficulty, seed=7).world


def test_a_world_snapshot_plus_the_delta_is_the_task_world(tmp_path):
    base = generate_mid_market_company(11, SIZE)
    task = task_world(base)
    delta = world_delta(base, task)
    assert len(delta.emails) == 3 and [document.path for document in delta.documents] == ["invoices.csv"]
    assert delta.people == task.people and delta.invoices == task.invoices
    assert World.model_validate(delta.model_dump(mode="json")) == delta
    snapshotted = tmp_path / "snapshotted"
    materialize_world(base, APPS, snapshotted)
    materialize_world(delta, APPS, snapshotted)
    fresh = tmp_path / "fresh"
    materialize_world(task, APPS, fresh)
    assert extract_world_state(APPS, snapshotted) == extract_world_state(APPS, fresh)


def test_an_unchanged_world_has_nothing_to_inject():
    base = generate_mid_market_company(11, SIZE)
    delta = world_delta(base, base)
    assert (delta.emails, delta.documents, delta.chat_messages, delta.events) == ((), (), (), ())


def test_tasks_that_remove_or_move_worlds_cannot_start_from_the_snapshot():
    base = generate_mid_market_company(11, SIZE)
    assert world_delta(base, base.model_copy(update={"emails": base.emails[1:]})) is None
    assert world_delta(base, base.model_copy(update={"customers": base.customers[1:]})) is None
    assert world_delta(base, base.model_copy(update={"seed": base.seed + 1})) is None
    assert world_delta(base, generate_mid_market_company(12, SIZE)) is None
