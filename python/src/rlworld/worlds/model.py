"""World model: one coherent synthetic organization (DESIGN.md section 6.4).

Every record references others by id and the validator rejects dangling
references, so the mailbox, the files and the CRM always agree with each other.
All names and domains are synthetic; domains use the reserved .example suffix.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, model_validator

from rlworld.specs.digest import content_digest

SYNTHETIC_DOMAIN_SUFFIX = ".example"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Person(FrozenModel):
    id: str
    full_name: str
    email: str
    role: str
    department: str
    manager_id: str | None = None


class Customer(FrozenModel):
    id: str
    name: str
    domain: str
    industry: str
    contact_name: str
    contact_email: str


class Invoice(FrozenModel):
    id: str
    customer_id: str
    amount: float = Field(gt=0)
    currency: str = "USD"
    issued_on: date
    due_on: date
    status: Literal["paid", "open", "overdue"]


class Deal(FrozenModel):
    id: str
    customer_id: str
    owner_id: str
    stage: Literal["prospect", "proposal", "negotiation", "won", "lost"]
    amount: float = Field(gt=0)


class MailMessage(FrozenModel):
    id: str
    thread_id: str
    sender: str
    recipients: tuple[str, ...] = Field(min_length=1)
    subject: str
    body: str
    sent_at: datetime
    is_draft: bool = False


class ChatMessage(FrozenModel):
    id: str
    channel: str
    sender_id: str
    text: str
    sent_at: datetime


class Document(FrozenModel):
    path: str = Field(pattern=r"^[A-Za-z0-9_.]+(/[A-Za-z0-9_.]+)*$")
    kind: Literal["csv", "text"]
    content: str


class WorldEvent(FrozenModel):
    """Something that happens mid task, such as an email arriving or a colleague replying."""

    id: str
    trigger_step: NonNegativeInt
    app: str
    kind: Literal["email_arrives", "chat_reply"]
    payload: dict[str, Any]


class World(FrozenModel):
    generator: str
    seed: int
    base_time: datetime
    organization_name: str
    domain: str
    agent_persona_id: str
    people: tuple[Person, ...]
    customers: tuple[Customer, ...] = ()
    invoices: tuple[Invoice, ...] = ()
    deals: tuple[Deal, ...] = ()
    emails: tuple[MailMessage, ...] = ()
    chat_messages: tuple[ChatMessage, ...] = ()
    documents: tuple[Document, ...] = ()
    events: tuple[WorldEvent, ...] = ()

    @model_validator(mode="after")
    def check_coherence(self) -> "World":
        problems = coherence_problems(self)
        if problems:
            raise ValueError("; ".join(problems[:10]))
        return self

    def person(self, person_id: str) -> Person:
        return next(person for person in self.people if person.id == person_id)

    @property
    def agent_persona(self) -> Person:
        return self.person(self.agent_persona_id)


def duplicate_ids(records: tuple[Any, ...], label: str) -> list[str]:
    seen_ids: set[str] = set()
    problems = []
    for record in records:
        record_id = getattr(record, "id", None) or getattr(record, "path", None)
        if record_id in seen_ids:
            problems.append(f"duplicate {label} {record_id}")
        seen_ids.add(record_id)
    return problems


def coherence_problems(world: World) -> list[str]:
    problems = []
    for records, label in ((world.people, "person"), (world.customers, "customer"), (world.invoices, "invoice"),
                           (world.deals, "deal"), (world.emails, "email"), (world.chat_messages, "chat message"),
                           (world.documents, "document"), (world.events, "event")):
        problems.extend(duplicate_ids(records, label))
    person_ids = {person.id for person in world.people}
    customer_ids = {customer.id for customer in world.customers}
    known_addresses = ({person.email for person in world.people}
                       | {customer.contact_email for customer in world.customers})
    if world.agent_persona_id not in person_ids:
        problems.append(f"agent persona {world.agent_persona_id} is not a person")
    problems.extend(
        f"person {person.id} reports to unknown {person.manager_id}"
        for person in world.people
        if person.manager_id is not None and person.manager_id not in person_ids
    )
    problems.extend(f"invoice {invoice.id} has unknown customer {invoice.customer_id}"
                    for invoice in world.invoices if invoice.customer_id not in customer_ids)
    problems.extend(f"deal {deal.id} has unknown customer or owner"
                    for deal in world.deals if deal.customer_id not in customer_ids or deal.owner_id not in person_ids)
    for message in world.emails:
        unknown_addresses = [address for address in (message.sender, *message.recipients)
                             if address not in known_addresses]
        if unknown_addresses:
            problems.append(f"email {message.id} uses unknown addresses {unknown_addresses}")
    problems.extend(f"chat message {chat.id} has unknown sender {chat.sender_id}"
                    for chat in world.chat_messages if chat.sender_id not in person_ids)
    synthetic_domains = [world.domain, *(customer.domain for customer in world.customers)]
    problems.extend(f"domain {domain} is not synthetic"
                    for domain in synthetic_domains if not domain.endswith(SYNTHETIC_DOMAIN_SUFFIX))
    return problems


def world_digest(world: World) -> str:
    return content_digest(world.model_dump(mode="json"))
