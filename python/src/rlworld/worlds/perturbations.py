"""Perturbations and scripted events (DESIGN.md section 6.4).

Distractors add irrelevant records without touching anything a task depends
on. Corrupted inputs damage rows of the invoice export the way real exports
get damaged and the log says exactly what changed so task generators can
build ground truth around it.
"""

import csv
import io
import random
from dataclasses import dataclass
from datetime import timedelta

from rlworld.worlds.model import Document, MailMessage, World, WorldEvent

INVOICE_EXPORT_PATH = "invoices.csv"
DISTRACTOR_SUBJECTS = (
    "Team lunch on Thursday",
    "Parking garage maintenance",
    "New coffee machine on floor 3",
    "Reminder: update your password",
    "Quarterly town hall recording",
)


@dataclass(frozen=True)
class Corruption:
    record_id: str
    field_name: str
    original_value: str
    corrupted_value: str


@dataclass(frozen=True)
class PerturbationLog:
    distractor_ids: tuple[str, ...]
    corruptions: tuple[Corruption, ...]


def corrupt_amount(amount_text: str, random_generator: random.Random) -> str:
    """Formatting damage a real export suffers: thousands separators, comma decimals, stray currency."""
    amount = float(amount_text)
    damaged_forms = (f"{amount:,.2f}", f"{amount:.2f}".replace(".", ","), f"$ {amount:.2f} ", f"{amount:.2f}0 ")
    return random_generator.choice(damaged_forms)


def apply_perturbations(world: World, distractors: int, corrupted_inputs: int,
                        seed: int) -> tuple[World, PerturbationLog]:
    random_generator = random.Random(seed)
    persona_email = world.agent_persona.email
    colleagues = [person for person in world.people if person.id != world.agent_persona_id]
    distractor_emails = [
        MailMessage(
            id=f"mail_distractor_{index + 1:04d}",
            thread_id=f"thread_distractor_{index + 1:04d}",
            sender=random_generator.choice(colleagues).email,
            recipients=(persona_email,),
            subject=random_generator.choice(DISTRACTOR_SUBJECTS),
            body="FYI, no action needed.",
            sent_at=world.base_time - timedelta(hours=random_generator.randint(1, 72)),
        )
        for index in range(distractors)
    ]
    corruptions: list[Corruption] = []
    documents = list(world.documents)
    export_index = next((index for index, document in enumerate(documents)
                         if document.path == INVOICE_EXPORT_PATH), None)
    if corrupted_inputs and export_index is not None:
        rows = list(csv.reader(io.StringIO(documents[export_index].content)))
        header, data_rows = rows[0], rows[1:]
        amount_column = header.index("amount")
        for row_index in sorted(random_generator.sample(range(len(data_rows)), min(corrupted_inputs, len(data_rows)))):
            row = data_rows[row_index]
            corrupted_value = corrupt_amount(row[amount_column], random_generator)
            corruptions.append(Corruption(row[0], "amount", row[amount_column], corrupted_value))
            row[amount_column] = corrupted_value
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(data_rows)
        documents[export_index] = Document(path=INVOICE_EXPORT_PATH, kind="csv", content=buffer.getvalue())
    perturbed_world = world.model_copy(
        update={"emails": world.emails + tuple(distractor_emails), "documents": tuple(documents)}
    )
    log = PerturbationLog(tuple(message.id for message in distractor_emails), tuple(corruptions))
    return World.model_validate(perturbed_world.model_dump()), log


def events_due(world: World, step_index: int) -> list[WorldEvent]:
    return [event for event in world.events if event.trigger_step == step_index]


def event_message(event: WorldEvent, world: World) -> MailMessage:
    """The mail an email_arrives event delivers, stamped at the world's base time."""
    return MailMessage(
        id=f"mail_{event.id}",
        thread_id=f"thread_{event.id}",
        sender=event.payload["sender"],
        recipients=tuple(event.payload["recipients"]),
        subject=event.payload["subject"],
        body=event.payload["body"],
        sent_at=world.base_time,
    )
