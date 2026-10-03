import csv
import io

from rlworld.rewards.checks import parse_number
from rlworld.worlds import apply_perturbations, events_due, generate_mid_market_company

SIZE = {"employees": 8, "customers": 6, "emails_per_person": 2, "invoices_per_customer": 3}


def recover_amount(amount_text: str) -> float:
    """Undo a comma decimal such as 1250,00; parse_number handles the other damage."""
    is_comma_decimal = amount_text.count(",") == 1 and "." not in amount_text
    return parse_number(amount_text.replace(",", ".") if is_comma_decimal else amount_text)


def invoice_rows(world):
    export = next(document for document in world.documents if document.path == "invoices.csv")
    return list(csv.DictReader(io.StringIO(export.content)))


def test_distractors_add_mail_without_touching_tasks_inputs():
    world = generate_mid_market_company(5, SIZE)
    perturbed_world, log = apply_perturbations(world, distractors=4, corrupted_inputs=0, seed=1)
    assert len(perturbed_world.emails) == len(world.emails) + 4
    assert log.distractor_ids == tuple(f"mail_distractor_{index:04d}" for index in range(1, 5))
    assert perturbed_world.documents == world.documents
    assert perturbed_world.invoices == world.invoices


def test_corruptions_damage_formatting_but_keep_the_value_recoverable():
    world = generate_mid_market_company(5, SIZE)
    perturbed_world, log = apply_perturbations(world, distractors=0, corrupted_inputs=3, seed=2)
    assert len(log.corruptions) == 3
    rows_by_id = {row["invoice_id"]: row for row in invoice_rows(perturbed_world)}
    for corruption in log.corruptions:
        assert rows_by_id[corruption.record_id]["amount"] == corruption.corrupted_value
        assert corruption.corrupted_value != corruption.original_value
        assert recover_amount(corruption.corrupted_value) == float(corruption.original_value)
    untouched_ids = {row["invoice_id"] for row in invoice_rows(world)} - {item.record_id for item in log.corruptions}
    original_rows = {row["invoice_id"]: row for row in invoice_rows(world)}
    assert all(rows_by_id[invoice_id] == original_rows[invoice_id] for invoice_id in untouched_ids)


def test_perturbations_are_deterministic_per_seed():
    world = generate_mid_market_company(5, SIZE)
    assert apply_perturbations(world, 2, 2, seed=9) == apply_perturbations(world, 2, 2, seed=9)
    assert apply_perturbations(world, 2, 2, seed=9)[1] != apply_perturbations(world, 2, 2, seed=10)[1]


def test_events_fire_at_their_step():
    world = generate_mid_market_company(5, SIZE)
    assert events_due(world, 9) == []
    assert [event.id for event in events_due(world, 10)] == ["event_cfo_nudge"]
