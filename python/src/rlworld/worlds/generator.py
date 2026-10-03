"""Seeded world generators (DESIGN.md section 6.4).

The same generator, seed and size always produce the same world. Content is
template based so it is deterministic and cheap; an LLM enrichment pass can
rewrite bodies later as long as the result still validates.
"""

import csv
import io
import random
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone

from rlworld.specs.models import WorldSpec
from rlworld.worlds.model import (
    ChatMessage,
    Customer,
    Deal,
    Document,
    Invoice,
    MailMessage,
    Person,
    World,
    WorldEvent,
)

DEFAULT_BASE_TIME = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)
FIRST_NAMES = (
    "Ada", "Bram", "Cleo", "Dev", "Esme", "Femi", "Gus", "Hana", "Ivo", "Juno", "Kai", "Lena", "Milo", "Nia", "Oren",
    "Pia", "Quin", "Rosa", "Sami", "Tova", "Uri", "Vera", "Wes", "Xan", "Yara", "Zev", "Alba", "Bodhi", "Cyra",
    "Dario", "Elio", "Fern", "Gia", "Hugo", "Isla", "Jory", "Kira", "Lior", "Maya", "Nico",
)
LAST_NAMES = (
    "Abara", "Brandt", "Castell", "Dalby", "Eklund", "Farrow", "Garrod", "Holm", "Ibsen", "Jarvi", "Kessler", "Lund",
    "Marlow", "Nakai", "Orsini", "Penner", "Quade", "Rourke", "Solberg", "Tamsin", "Ulrich", "Varga", "Wexler",
    "Yandel", "Zorn", "Achebe", "Birke", "Corwin", "Dunmore", "Ellery",
)
COMPANY_PREFIXES = (
    "Amber", "Basalt", "Cobalt", "Driftwood", "Ember", "Fjord", "Granite", "Harbor", "Iris", "Juniper", "Kestrel",
    "Lumen", "Meridian", "Nimbus", "Orchard", "Pinnacle", "Quarry", "Riverstone", "Saffron", "Tundra", "Umber",
    "Verdant", "Willow", "Zephyr",
)
COMPANY_SUFFIXES = ("Holdings", "Labs", "Logistics", "Analytics", "Foods", "Robotics", "Health", "Textiles", "Energy")
INDUSTRIES = ("retail", "logistics", "healthcare", "manufacturing", "software", "energy", "food", "finance")
DEAL_STAGES = ("prospect", "proposal", "negotiation", "won", "lost")
TEAM_ROLES = (
    ("Finance", "Accountant"),
    ("Finance", "Accounts receivable clerk"),
    ("Sales", "Account executive"),
    ("Sales", "Sales operations analyst"),
    ("Operations", "Operations coordinator"),
    ("Operations", "Procurement specialist"),
)


class UnknownWorldGeneratorError(KeyError):
    """A world spec names a generator that is not registered."""


def slug(text: str) -> str:
    return "".join(character for character in text.lower() if character.isalnum())


def unique_name(random_generator: random.Random, used_names: set[str]) -> str:
    for _ in range(1000):
        candidate = f"{random_generator.choice(FIRST_NAMES)} {random_generator.choice(LAST_NAMES)}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
    candidate = f"{random_generator.choice(FIRST_NAMES)} {random_generator.choice(LAST_NAMES)} {len(used_names)}"
    used_names.add(candidate)
    return candidate


def generate_people(random_generator: random.Random, employee_count: int, domain: str) -> list[Person]:
    used_names: set[str] = set()
    leadership = [("person_001", "Chief executive officer", "Executive", None),
                  ("person_002", "Chief financial officer", "Finance", "person_001"),
                  ("person_003", "Finance manager", "Finance", "person_002"),
                  ("person_004", "Head of sales", "Sales", "person_001"),
                  ("person_005", "Head of operations", "Operations", "person_001")]
    department_heads = {"Finance": "person_003", "Sales": "person_004", "Operations": "person_005"}
    people = []
    for index in range(employee_count):
        full_name = unique_name(random_generator, used_names)
        email = f"{slug(full_name.split()[0])}.{slug(full_name.split()[1])}{index + 1}@{domain}"
        if index < len(leadership):
            person_id, role, department, manager_id = leadership[index]
        else:
            department, role = TEAM_ROLES[index % len(TEAM_ROLES)]
            person_id, manager_id = f"person_{index + 1:03d}", department_heads[department]
        people.append(Person(id=person_id, full_name=full_name, email=email, role=role, department=department,
                             manager_id=manager_id))
    return people


def generate_customers(random_generator: random.Random, customer_count: int) -> list[Customer]:
    customers = []
    used_company_names: set[str] = set()
    used_contact_names: set[str] = set()
    for index in range(customer_count):
        company_name = f"{random_generator.choice(COMPANY_PREFIXES)} {random_generator.choice(COMPANY_SUFFIXES)}"
        if company_name in used_company_names:
            company_name = f"{company_name} {index + 1}"
        used_company_names.add(company_name)
        customer_domain = f"{slug(company_name)}.example"
        contact_name = unique_name(random_generator, used_contact_names)
        customers.append(
            Customer(
                id=f"customer_{index + 1:04d}",
                name=company_name,
                domain=customer_domain,
                industry=random_generator.choice(INDUSTRIES),
                contact_name=contact_name,
                contact_email=f"{slug(contact_name.split()[0])}@{customer_domain}",
            )
        )
    return customers


def generate_invoices(random_generator: random.Random, customers: list[Customer], invoices_per_customer: int,
                      base_time: datetime) -> list[Invoice]:
    invoices = []
    for customer in customers:
        for _ in range(invoices_per_customer):
            issued_on = (base_time - timedelta(days=random_generator.randint(10, 90))).date()
            due_on = issued_on + timedelta(days=30)
            if due_on >= base_time.date():
                status = "open"
            else:
                status = "paid" if random_generator.random() < 0.7 else "overdue"
            invoices.append(
                Invoice(
                    id=f"INV-{1001 + len(invoices)}",
                    customer_id=customer.id,
                    amount=round(random_generator.uniform(200, 25_000), 2),
                    issued_on=issued_on,
                    due_on=due_on,
                    status=status,
                )
            )
    return invoices


def generate_deals(random_generator: random.Random, customers: list[Customer], people: list[Person]) -> list[Deal]:
    sales_people = [person for person in people if person.department == "Sales"]
    return [
        Deal(
            id=f"deal_{index + 1:04d}",
            customer_id=customer.id,
            owner_id=random_generator.choice(sales_people).id,
            stage=random_generator.choice(DEAL_STAGES),
            amount=round(random_generator.uniform(5_000, 250_000), 2),
        )
        for index, customer in enumerate(customers)
    ]


def generate_emails(random_generator: random.Random, people: list[Person], customers: list[Customer],
                    invoices: list[Invoice], emails_per_person: int, persona: Person,
                    base_time: datetime) -> list[MailMessage]:
    customers_by_id = {customer.id: customer for customer in customers}
    emails: list[MailMessage] = []
    for sender in people:
        for _ in range(emails_per_person):
            message_index = len(emails) + 1
            sent_at = base_time - timedelta(minutes=random_generator.randint(30, 60 * 24 * 60))
            if invoices and random_generator.random() < 0.5:
                invoice = random_generator.choice(invoices)
                customer = customers_by_id[invoice.customer_id]
                subject = f"Invoice {invoice.id} for {customer.name} is {invoice.status}"
                body = (
                    f"Hi {persona.full_name.split()[0]},\n\nInvoice {invoice.id} for {customer.name} "
                    f"({invoice.amount:,.2f} {invoice.currency}) is {invoice.status}, due {invoice.due_on}.\n\n"
                    f"{sender.full_name}"
                )
                recipients = (persona.email,) if sender.id != persona.id else (people[1].email,)
            else:
                colleague = random_generator.choice([person for person in people if person.id != sender.id])
                subject = f"Notes from the {sent_at:%A} sync"
                body = f"Hi {colleague.full_name.split()[0]},\n\nSharing my notes from the sync.\n\n{sender.full_name}"
                recipients = (colleague.email,)
            emails.append(
                MailMessage(id=f"mail_{message_index:06d}", thread_id=f"thread_{message_index:06d}",
                            sender=sender.email, recipients=recipients, subject=subject, body=body, sent_at=sent_at)
            )
    paid_invoices = [invoice for invoice in invoices if invoice.status == "paid"]
    for invoice in paid_invoices[: max(1, len(paid_invoices) // 10)]:
        customer = customers_by_id[invoice.customer_id]
        message_index = len(emails) + 1
        emails.append(
            MailMessage(
                id=f"mail_{message_index:06d}",
                thread_id=f"thread_{message_index:06d}",
                sender=customer.contact_email,
                recipients=(persona.email,),
                subject=f"Payment sent for {invoice.id}",
                body=f"Hello,\n\nWe paid invoice {invoice.id} in full.\n\n{customer.contact_name}, {customer.name}",
                sent_at=base_time - timedelta(days=random_generator.randint(1, 20)),
            )
        )
    return emails


def csv_document(path: str, header: list[str], rows: list[list[object]]) -> Document:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return Document(path=path, kind="csv", content=buffer.getvalue())


def generate_documents(customers: list[Customer], invoices: list[Invoice], persona: Person,
                       partial_workflows: int) -> list[Document]:
    customer_names = {customer.id: customer.name for customer in customers}
    documents = [
        csv_document(
            "invoices.csv",
            ["invoice_id", "customer", "amount", "issued_on", "due_on", "status"],
            [[invoice.id, customer_names[invoice.customer_id], f"{invoice.amount:.2f}", invoice.issued_on,
              invoice.due_on, invoice.status] for invoice in invoices],
        ),
        csv_document(
            "customers.csv",
            ["customer_id", "name", "industry", "contact_email"],
            [[customer.id, customer.name, customer.industry, customer.contact_email] for customer in customers],
        ),
    ]
    if partial_workflows:
        documents.append(
            Document(
                path="q1_reconciliation_notes.md",
                kind="text",
                content=(
                    f"# Q1 reconciliation ({persona.full_name})\n\n"
                    "- [x] Export invoices\n- [ ] Match payments against invoices\n- [ ] Email the CFO a summary\n"
                ),
            )
        )
    return documents


def generate_mid_market_company(seed: int, size: Mapping[str, int], base_time: datetime | None = None) -> World:
    random_generator = random.Random(seed)
    world_time = base_time or DEFAULT_BASE_TIME
    employee_count = max(5, size.get("employees", 12))
    company_name = f"{random_generator.choice(COMPANY_PREFIXES)} {random_generator.choice(COMPANY_SUFFIXES)}"
    domain = f"{slug(company_name)}.example"
    people = generate_people(random_generator, employee_count, domain)
    persona = people[2]
    customers = generate_customers(random_generator, size.get("customers", 20))
    invoices = generate_invoices(random_generator, customers, size.get("invoices_per_customer", 2), world_time)
    deals = generate_deals(random_generator, customers, people)
    emails = generate_emails(random_generator, people, customers, invoices, size.get("emails_per_person", 5), persona,
                             world_time)
    partial_workflows = size.get("partial_workflows", 1)
    if partial_workflows:
        emails.append(
            MailMessage(id="mail_draft_000001", thread_id="thread_draft_000001", sender=persona.email,
                        recipients=(people[1].email,), subject="Q1 reconciliation summary",
                        body="Hi,\n\nReconciliation status so far:\n", sent_at=world_time, is_draft=True)
        )
    chat_messages = [
        ChatMessage(id=f"chat_{index + 1:05d}", channel="finance", sender_id=random_generator.choice(people).id,
                    text="Reminder: month end close is Friday.", sent_at=world_time - timedelta(hours=index + 1))
        for index in range(size.get("chat_messages", 3))
    ]
    events = (
        WorldEvent(id="event_cfo_nudge", trigger_step=10, app="mail", kind="email_arrives",
                   payload={"sender": people[1].email, "recipients": [persona.email],
                            "subject": "Where are we on reconciliation?", "body": "Need the summary today."}),
    ) if size.get("scripted_events", 1) else ()
    return World(
        generator="worlds.mid_market_company",
        seed=seed,
        base_time=world_time,
        organization_name=company_name,
        domain=domain,
        agent_persona_id=persona.id,
        people=tuple(people),
        customers=tuple(customers),
        invoices=tuple(invoices),
        deals=tuple(deals),
        emails=tuple(emails),
        chat_messages=tuple(chat_messages),
        documents=tuple(generate_documents(customers, invoices, persona, partial_workflows)),
        events=events,
    )


WORLD_GENERATORS: dict[str, Callable[[int, Mapping[str, int], datetime | None], World]] = {
    "worlds.mid_market_company": generate_mid_market_company,
}


def build_world(world_spec: WorldSpec) -> World:
    generator_function = WORLD_GENERATORS.get(world_spec.generator)
    if generator_function is None:
        raise UnknownWorldGeneratorError(world_spec.generator)
    fixed_time = world_spec.clock.fixed
    if fixed_time is not None and fixed_time.tzinfo is None:
        fixed_time = fixed_time.replace(tzinfo=timezone.utc)
    return generator_function(world_spec.seed, world_spec.size, fixed_time)
