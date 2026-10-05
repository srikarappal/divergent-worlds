"""App integration contract and reference integrations (DESIGN.md section 6.4).

Every app ships three operations: materialize writes world state into the app,
extract reads normalized state back for verifiers and diffs, reset empties it.
Apps that take world events mid task also deliver one record (an email that
arrives or a colleague's chat reply) and name the state path it lands on. The
reference integrations below target a local directory (Maildir style mail, one
JSON file per chat message and plain files); in a VM the same contract runs
through the guest agent or a sidecar such as an IMAP server. Spreadsheets are
read the way section 6.9 asks for Excel: the values the app last computed,
through openpyxl.
"""

import csv
import hashlib
import io
import json
import mailbox
import re
import shutil
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, parsedate_to_datetime
from operator import itemgetter
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel, ValidationError

from rlworld.worlds.model import ChatMessage, MailMessage, World

MAIL_FOLDERS = ("inbox", "sent", "drafts")
# Mail clients and Dovecot reach the agent persona's mailbox through this link beside the per address Maildirs.
CURRENT_MAILBOX = "current"
PEOPLE_FILE = "people.json"
MESSAGE_ID_DOMAIN = "rlworld.example"
MESSAGE_ID_PATTERN = re.compile(r"\s*<?([^@<>\s]+)@")
MAX_EXTRACTED_TEXT_BYTES = 64 * 1024
SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm")
# A guest keeps its world in the desktop user's home under this folder, where both the apps and the policy reach it.
USER_WORLD_FOLDER = "world"


class UnsupportedAppError(KeyError):
    """A world asks to materialize an app that has no integration yet."""


class AppIntegration(Protocol):
    app_name: str

    def materialize(self, world: World, target: Path) -> None: ...

    def extract(self, target: Path) -> dict[str, Any]: ...

    def reset(self, target: Path) -> None: ...


@runtime_checkable
class DeliveringIntegration(AppIntegration, Protocol):
    """An app a record can land in mid task; record_model validates what arrives."""

    record_model: type[BaseModel]

    def deliver(self, record: Any, target: Path) -> None: ...

    def delivered_path(self, record: Any) -> str: ...


def email_message(message: MailMessage) -> EmailMessage:
    email = EmailMessage()
    email["Message-ID"] = f"<{message.id}@{MESSAGE_ID_DOMAIN}>"
    email["X-Thread-Id"] = message.thread_id
    email["From"] = message.sender
    email["To"] = ", ".join(message.recipients)
    email["Subject"] = message.subject
    email["Date"] = format_datetime(message.sent_at)
    email.set_content(message.body)
    return email


def maildir_folder(folder_directory: Path) -> mailbox.Maildir:
    """The Maildir folder, created with its parents; mailbox only creates the last level."""
    folder_directory.parent.mkdir(parents=True, exist_ok=True)
    return mailbox.Maildir(folder_directory, create=True)


def write_message(message: MailMessage, folder_directory: Path, flags: str = "") -> None:
    """Into a Maildir folder: unread mail lands in new, mail with flags such as S for seen in cur."""
    maildir_message = mailbox.MaildirMessage(email_message(message))
    if flags:
        maildir_message.set_subdir("cur")
        maildir_message.set_flags(flags)
    maildir_folder(folder_directory).add(maildir_message)


def message_id(header_value: str, fallback: str) -> str:
    """A message's id is the local part of its Message-ID, so mail written by clients keeps a stable id too."""
    match = MESSAGE_ID_PATTERN.match(header_value)
    return match.group(1) if match else fallback


def read_message(message_bytes: bytes, fallback_id: str) -> dict[str, Any]:
    email = BytesParser(policy=policy.default).parsebytes(message_bytes)
    body = email.get_body(preferencelist=("plain", "html"))
    return {
        "id": message_id(str(email["Message-ID"] or ""), fallback_id),
        "thread_id": str(email["X-Thread-Id"] or ""),
        "from": str(email["From"]),
        "to": [address.strip() for address in str(email["To"] or "").split(",") if address.strip()],
        "subject": str(email["Subject"] or ""),
        "body": body.get_content().rstrip("\n") if body is not None else "",
        "date": parsedate_to_datetime(str(email["Date"])).isoformat() if email["Date"] else "",
    }


def read_folder(folder_directory: Path) -> list[dict[str, Any]]:
    if not folder_directory.is_dir():
        return []
    maildir = mailbox.Maildir(folder_directory, factory=None, create=False)
    messages = [read_message(maildir.get_bytes(key), key) for key in maildir.keys()]
    return sorted(messages, key=itemgetter("id"))


class MailIntegration:
    """A Maildir per address under mail/<address>/<folder>/; mail/current links the agent persona's mailbox, which
    is the one Dovecot serves to mail clients and the one extract reads."""

    app_name = "mail"
    record_model = MailMessage

    def mail_root(self, target: Path) -> Path:
        return target / "mail"

    def materialize(self, world: World, target: Path) -> None:
        mail_root = self.mail_root(target)
        owner = world.agent_persona
        for folder in MAIL_FOLDERS:
            maildir_folder(mail_root / owner.email / folder)
        (mail_root / "owner.txt").write_text(owner.email)
        (mail_root / "owner_name.txt").write_text(owner.full_name)
        current = mail_root / CURRENT_MAILBOX
        current.unlink(missing_ok=True)
        current.symlink_to(owner.email)
        for message in world.emails:
            if message.is_draft:
                write_message(message, mail_root / message.sender / "drafts", "DS")
                continue
            write_message(message, mail_root / message.sender / "sent", "S")
            for recipient in message.recipients:
                write_message(message, mail_root / recipient / "inbox")

    def deliver(self, message: MailMessage, target: Path) -> None:
        """A scripted world event: the message lands unread in each recipient's inbox mid task."""
        for recipient in message.recipients:
            write_message(message, self.mail_root(target) / recipient / "inbox")

    def delivered_path(self, message: MailMessage) -> str:
        return f"{self.app_name}/inbox/{message.id}"

    def extract(self, target: Path) -> dict[str, Any]:
        mail_root = self.mail_root(target)
        owner_path = mail_root / "owner.txt"
        if not owner_path.is_file():
            return {folder: [] for folder in MAIL_FOLDERS}
        owner_mailbox = mail_root / owner_path.read_text().strip()
        return {folder: read_folder(owner_mailbox / folder) for folder in MAIL_FOLDERS}

    def reset(self, target: Path) -> None:
        shutil.rmtree(self.mail_root(target), ignore_errors=True)


def cell_value(value: Any) -> Any:
    """Cell values as JSON: dates and times become ISO text."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def spreadsheet_cells(content: bytes) -> tuple[str, dict[str, dict[str, Any]]]:
    """The first sheet's non empty cells with the values the app last computed, keyed like B2."""
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    try:
        sheet = workbook.worksheets[0]
        cells = {cell.coordinate: {"value": cell_value(cell.value)}
                 for row in sheet.iter_rows() for cell in row if cell.value is not None}
        return sheet.title, cells
    finally:
        workbook.close()


def describe_file(file_path: Path) -> dict[str, Any]:
    content = file_path.read_bytes()
    description: dict[str, Any] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    if file_path.suffix.lower() in SPREADSHEET_SUFFIXES:
        try:
            sheet_name, cells = spreadsheet_cells(content)
        except (InvalidFileException, zipfile.BadZipFile, KeyError, ValueError, OSError):
            description["kind"] = "binary"
        else:
            description.update(kind="spreadsheet", sheet=sheet_name, cells=cells)
    elif file_path.suffix == ".csv":
        description["kind"] = "csv"
        description["rows"] = list(csv.DictReader(io.StringIO(content.decode("utf-8", errors="replace"))))
    elif file_path.suffix in (".txt", ".md") and len(content) <= MAX_EXTRACTED_TEXT_BYTES:
        description["kind"] = "text"
        description["text"] = content.decode("utf-8", errors="replace")
    else:
        description["kind"] = "binary"
    return description


class FilesIntegration:
    """Plain files under files/; extract keys each file by its path relative to that folder."""

    app_name = "files"

    def files_root(self, target: Path) -> Path:
        return target / "files"

    def materialize(self, world: World, target: Path) -> None:
        for document in world.documents:
            document_path = self.files_root(target) / document.path
            document_path.parent.mkdir(parents=True, exist_ok=True)
            document_path.write_text(document.content)

    def extract(self, target: Path) -> dict[str, Any]:
        files_root = self.files_root(target)
        if not files_root.is_dir():
            return {}
        return {
            file_path.relative_to(files_root).as_posix(): describe_file(file_path)
            for file_path in sorted(files_root.rglob("*"))
            if file_path.is_file()
        }

    def reset(self, target: Path) -> None:
        shutil.rmtree(self.files_root(target), ignore_errors=True)


def direct_channel(person_id: str) -> str:
    """The channel holding the agent's direct messages with one colleague."""
    return f"dm_{person_id}"


def utc_time(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


class ChatIntegration:
    """One JSON file per message under chat/<channel>/; owner.txt names the agent persona and people.json who is who.

    The folder names the channel and the file name the message, whatever the file says; a file that is not a
    message is skipped, so a malformed post the policy wrote counts as no post.
    """

    app_name = "chat"
    record_model = ChatMessage

    def chat_root(self, target: Path) -> Path:
        return target / "chat"

    def write(self, message: ChatMessage, target: Path) -> None:
        channel_directory = self.chat_root(target) / message.channel
        channel_directory.mkdir(parents=True, exist_ok=True)
        (channel_directory / f"{message.id}.json").write_text(message.model_dump_json())

    def materialize(self, world: World, target: Path) -> None:
        chat_root = self.chat_root(target)
        chat_root.mkdir(parents=True, exist_ok=True)
        (chat_root / "owner.txt").write_text(world.agent_persona_id)
        people = {person.id: {"name": person.full_name, "role": person.role} for person in world.people}
        (chat_root / PEOPLE_FILE).write_text(json.dumps(people, indent=2, sort_keys=True))
        for message in world.chat_messages:
            self.write(message, target)

    def deliver(self, message: ChatMessage, target: Path) -> None:
        self.write(message, target)

    def delivered_path(self, message: ChatMessage) -> str:
        return f"{self.app_name}/{message.channel}/{message.id}"

    def messages(self, target: Path) -> list[ChatMessage]:
        messages = []
        for message_path in self.chat_root(target).glob("*/*.json"):
            try:
                record = json.loads(message_path.read_text())
                messages.append(ChatMessage.model_validate({**record, "id": message_path.stem,
                                                            "channel": message_path.parent.name}))
            except (OSError, ValueError, TypeError, ValidationError):
                continue
        return sorted(messages, key=chat_order)

    def owner_posts(self, target: Path) -> list[ChatMessage]:
        """The agent persona's own messages, which colleagues may answer."""
        owner_path = self.chat_root(target) / "owner.txt"
        if not owner_path.is_file():
            return []
        owner = owner_path.read_text().strip()
        return [message for message in self.messages(target) if message.sender_id == owner]

    def extract(self, target: Path) -> dict[str, Any]:
        channels: dict[str, list[dict[str, Any]]] = {}
        for message in self.messages(target):
            channels.setdefault(message.channel, []).append(
                {"id": message.id, "sender_id": message.sender_id, "text": message.text,
                 "sent_at": utc_time(message.sent_at).isoformat()})
        return channels

    def reset(self, target: Path) -> None:
        shutil.rmtree(self.chat_root(target), ignore_errors=True)


def chat_order(message: ChatMessage) -> tuple[datetime, str]:
    return utc_time(message.sent_at), message.id


DEFAULT_INTEGRATIONS: dict[str, AppIntegration] = {
    MailIntegration.app_name: MailIntegration(),
    FilesIntegration.app_name: FilesIntegration(),
    ChatIntegration.app_name: ChatIntegration(),
}


def resolve_integrations(app_names: Sequence[str], integrations: Mapping[str, AppIntegration]) -> list[AppIntegration]:
    missing_apps = [app_name for app_name in app_names if app_name not in integrations]
    if missing_apps:
        raise UnsupportedAppError(f"no integration for {', '.join(missing_apps)}")
    return [integrations[app_name] for app_name in app_names]


def materialize_world(world: World, app_names: Sequence[str], target: Path,
                      integrations: Mapping[str, AppIntegration] | None = None) -> None:
    for integration in resolve_integrations(app_names, integrations or DEFAULT_INTEGRATIONS):
        integration.materialize(world, target)


def extract_world_state(app_names: Sequence[str], target: Path,
                        integrations: Mapping[str, AppIntegration] | None = None) -> dict[str, Any]:
    """Normalized state per app, the input to world diffs, side effect checks and verifiers."""
    return {
        integration.app_name: integration.extract(target)
        for integration in resolve_integrations(app_names, integrations or DEFAULT_INTEGRATIONS)
    }


def reset_world(app_names: Sequence[str], target: Path,
                integrations: Mapping[str, AppIntegration] | None = None) -> None:
    for integration in resolve_integrations(app_names, integrations or DEFAULT_INTEGRATIONS):
        integration.reset(target)


def delivering_integration(app_name: str, integrations: Mapping[str, AppIntegration] | None) -> DeliveringIntegration:
    integration = resolve_integrations([app_name], integrations or DEFAULT_INTEGRATIONS)[0]
    if not isinstance(integration, DeliveringIntegration):
        raise UnsupportedAppError(f"{app_name} takes no records mid task")
    return integration


def deliver_record(app_name: str, record: Mapping[str, Any], target: Path,
                   integrations: Mapping[str, AppIntegration] | None = None) -> None:
    """A world event or a colleague's reply landing in a running app."""
    integration = delivering_integration(app_name, integrations)
    integration.deliver(integration.record_model.model_validate(record), target)


def delivered_path(app_name: str, record: Mapping[str, Any],
                   integrations: Mapping[str, AppIntegration] | None = None) -> str:
    """The state path a delivered record shows up at, which the world changed and the agent did not."""
    integration = delivering_integration(app_name, integrations)
    return integration.delivered_path(integration.record_model.model_validate(record))
