import re
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from rlworld.specs import parse_spec_document, set_path
from rlworld.worlds import UnknownWorldGeneratorError, World, build_world, generate_mid_market_company, world_digest

SMALL_SIZE = {"employees": 12, "customers": 8, "emails_per_person": 4, "invoices_per_customer": 2}


def test_the_same_seed_gives_the_same_world():
    first_world = generate_mid_market_company(7, SMALL_SIZE)
    assert generate_mid_market_company(7, SMALL_SIZE) == first_world
    assert world_digest(generate_mid_market_company(7, SMALL_SIZE)) == world_digest(first_world)
    assert world_digest(generate_mid_market_company(8, SMALL_SIZE)) != world_digest(first_world)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", world_digest(first_world))


def test_sizes_roles_and_persona():
    world = generate_mid_market_company(7, SMALL_SIZE)
    assert len(world.people) == 12
    assert len(world.customers) == 8
    assert len(world.invoices) == 16
    assert [person.role for person in world.people[:3]] == [
        "Chief executive officer", "Chief financial officer", "Finance manager"]
    assert world.agent_persona.role == "Finance manager"
    sent_by_people = [message for message in world.emails
                      if message.sender in {person.email for person in world.people} and not message.is_draft]
    assert len(sent_by_people) == 12 * 4


def test_worlds_are_coherent_and_synthetic():
    world = generate_mid_market_company(11, SMALL_SIZE)
    invoice_ids = {invoice.id for invoice in world.invoices}
    for message in world.emails:
        for referenced_invoice in re.findall(r"INV-\d+", message.subject + message.body):
            assert referenced_invoice in invoice_ids
    assert world.domain.endswith(".example")
    assert all(customer.contact_email.endswith(".example") for customer in world.customers)
    assert {document.path for document in world.documents} == {
        "invoices.csv", "customers.csv", "q1_reconciliation_notes.md"}
    assert any(message.is_draft for message in world.emails)
    assert world.events[0].kind == "email_arrives"


def test_dangling_references_are_rejected():
    world = generate_mid_market_company(7, SMALL_SIZE)
    broken_invoice = world.invoices[0].model_copy(update={"customer_id": "customer_9999"})
    broken_payload = world.model_copy(update={"invoices": (broken_invoice, *world.invoices[1:])}).model_dump()
    with pytest.raises(ValidationError) as raised:
        World.model_validate(broken_payload)
    assert "unknown customer customer_9999" in str(raised.value)


def test_real_looking_domains_are_rejected():
    world = generate_mid_market_company(7, SMALL_SIZE)
    with pytest.raises(ValidationError):
        World.model_validate({**world.model_dump(), "domain": "realcompany.com"})


def test_build_world_from_a_spec(example_payload):
    world_spec = parse_spec_document(example_payload).world
    world = build_world(world_spec)
    assert (world.seed, len(world.people), len(world.customers)) == (1842, 40, 300)
    assert world.base_time == datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)
    unknown_spec = parse_spec_document(set_path(example_payload, "world.generator", "worlds.space_station")).world
    with pytest.raises(UnknownWorldGeneratorError):
        build_world(unknown_spec)
