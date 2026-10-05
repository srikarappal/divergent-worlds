import mailbox
from datetime import datetime
from email.message import EmailMessage

import openpyxl
import pytest

from rlworld.rewards import diff_states, side_effect_violations
from rlworld.worlds import (
    MailIntegration,
    UnsupportedAppError,
    event_message,
    extract_world_state,
    generate_mid_market_company,
    materialize_world,
    reset_world,
)
from rlworld.worlds.integrations import write_message
from rlworld.worlds.model import MailMessage

SIZE = {"employees": 8, "customers": 5, "emails_per_person": 3}


@pytest.fixture
def world():
    return generate_mid_market_company(3, SIZE)


def test_mail_round_trips_through_the_persona_mailbox(world, tmp_path):
    materialize_world(world, ["mail"], tmp_path)
    mail_state = extract_world_state(["mail"], tmp_path)["mail"]
    persona_email = world.agent_persona.email
    expected_inbox = sorted(message.id for message in world.emails
                            if persona_email in message.recipients and not message.is_draft)
    expected_sent = sorted(message.id for message in world.emails
                           if message.sender == persona_email and not message.is_draft)
    assert [message["id"] for message in mail_state["inbox"]] == expected_inbox
    assert [message["id"] for message in mail_state["sent"]] == expected_sent
    assert [message["id"] for message in mail_state["drafts"]] == ["mail_draft_000001"]
    first_message = next(message for message in world.emails if message.id == expected_inbox[0])
    extracted_message = mail_state["inbox"][0]
    assert (extracted_message["subject"], extracted_message["body"]) == (first_message.subject, first_message.body)
    assert extracted_message["to"] == list(first_message.recipients)


def test_mailboxes_are_maildirs_with_the_persona_linked_as_current(world, tmp_path):
    materialize_world(world, ["mail"], tmp_path)
    persona = world.agent_persona
    mail_root = tmp_path / "mail"
    assert (mail_root / "current").resolve() == (mail_root / persona.email).resolve()
    assert (mail_root / "owner_name.txt").read_text() == persona.full_name
    inbox = mailbox.Maildir(mail_root / persona.email / "inbox", factory=None, create=False)
    assert inbox.keys() and all(inbox.get_message(key).get_subdir() == "new" for key in inbox.keys())
    sent = mailbox.Maildir(mail_root / persona.email / "sent", factory=None, create=False)
    assert all(sent.get_message(key).get_flags() == "S" for key in sent.keys())
    drafts = mailbox.Maildir(mail_root / persona.email / "drafts", factory=None, create=False)
    assert [drafts.get_message(key).get_flags() for key in drafts.keys()] == ["DS"]
    materialize_world(world, ["mail"], tmp_path)
    assert (mail_root / "current").resolve() == (mail_root / persona.email).resolve()


def test_mail_a_client_sent_reads_back_by_its_message_id(world, tmp_path):
    materialize_world(world, ["mail"], tmp_path)
    client_message = EmailMessage()
    client_message["Message-ID"] = "<4f2c9a7e-1b3d-4e5f@fjordlogistics.example>"
    client_message["From"] = world.agent_persona.email
    client_message["To"] = "cfo@fjordlogistics.example"
    client_message["Subject"] = "December reconciliation"
    client_message["Date"] = "Sat, 14 Mar 2026 10:15:00 +0000"
    client_message.set_content("Totals attached.")
    client_message.add_alternative("<p>Totals attached.</p>", subtype="html")
    sent_message = mailbox.MaildirMessage(client_message)
    sent_message.set_subdir("cur")
    sent_message.set_flags("S")
    mailbox.Maildir(tmp_path / "mail" / "current" / "sent", create=False).add(sent_message)
    sent = extract_world_state(["mail"], tmp_path)["mail"]["sent"]
    client_copy = next(message for message in sent if message["id"] == "4f2c9a7e-1b3d-4e5f")
    assert (client_copy["to"], client_copy["body"]) == (["cfo@fjordlogistics.example"], "Totals attached.")


def test_files_extract_csv_rows_and_text(world, tmp_path):
    materialize_world(world, ["files"], tmp_path)
    files_state = extract_world_state(["files"], tmp_path)["files"]
    assert set(files_state) == {"invoices.csv", "customers.csv", "q1_reconciliation_notes.md"}
    assert len(files_state["invoices.csv"]["rows"]) == len(world.invoices)
    assert files_state["invoices.csv"]["rows"][0]["invoice_id"] == world.invoices[0].id
    assert files_state["q1_reconciliation_notes.md"]["text"].startswith("# Q1 reconciliation")


def test_spreadsheets_extract_the_values_the_app_computed(world, tmp_path):
    materialize_world(world, ["files"], tmp_path)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Reconciliation"
    sheet["A1"], sheet["B1"], sheet["C3"] = "open", 1234.5, datetime(2026, 3, 1, 9, 30)
    workbook.save(tmp_path / "files" / "reconciliation.xlsx")
    (tmp_path / "files" / "broken.xlsx").write_bytes(b"not a zip")
    files_state = extract_world_state(["files"], tmp_path)["files"]
    spreadsheet = files_state["reconciliation.xlsx"]
    assert spreadsheet["kind"] == "spreadsheet" and spreadsheet["sheet"] == "Reconciliation"
    assert spreadsheet["cells"] == {"A1": {"value": "open"}, "B1": {"value": 1234.5},
                                    "C3": {"value": "2026-03-01T09:30:00"}}
    assert files_state["broken.xlsx"]["kind"] == "binary"


def test_reset_empties_every_app(world, tmp_path):
    materialize_world(world, ["mail", "files"], tmp_path)
    reset_world(["mail", "files"], tmp_path)
    assert extract_world_state(["mail", "files"], tmp_path) == {
        "mail": {"inbox": [], "sent": [], "drafts": []}, "files": {}}


def test_unsupported_apps_are_named(world, tmp_path):
    with pytest.raises(UnsupportedAppError) as raised:
        materialize_world(world, ["mail", "crm", "calendar"], tmp_path)
    assert "crm, calendar" in str(raised.value)


def test_agent_work_shows_up_as_allowed_changes(world, tmp_path):
    materialize_world(world, ["mail", "files"], tmp_path)
    initial_state = extract_world_state(["mail", "files"], tmp_path)
    persona = world.agent_persona
    summary_email = MailMessage(id="mail_agent_summary", thread_id="thread_agent", sender=persona.email,
                                recipients=(world.people[1].email,), subject="Q1 summary", body="All reconciled.",
                                sent_at=world.base_time)
    write_message(summary_email, tmp_path / "mail" / persona.email / "sent")
    (tmp_path / "files" / "customers.csv").unlink()
    final_state = extract_world_state(["mail", "files"], tmp_path)
    violations = side_effect_violations(diff_states(initial_state, final_state), ["mail/sent"])
    assert [violation.path for violation in violations] == ["files/customers.csv"]


def test_scripted_events_deliver_mail_mid_task(world, tmp_path):
    materialize_world(world, ["mail"], tmp_path)
    inbox_before = len(extract_world_state(["mail"], tmp_path)["mail"]["inbox"])
    MailIntegration().deliver(event_message(world.events[0], world), tmp_path)
    inbox_after = extract_world_state(["mail"], tmp_path)["mail"]["inbox"]
    assert len(inbox_after) == inbox_before + 1
    assert any(message["subject"] == "Where are we on reconciliation?" for message in inbox_after)
