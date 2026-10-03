"""App integration contract and reference integrations (DESIGN.md section 6.4).

Every app ships three operations: materialize writes world state into the app,
extract reads normalized state back for verifiers and diffs, reset empties it.
The reference integrations below target a local directory (Maildir style mail
and plain files); in a VM the same contract runs through the guest agent or a
sidecar such as an IMAP server.
"""

import csv
import hashlib
import io
import shutil
from collections.abc import Mapping, Sequence
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from rlworld.worlds.model import MailMessage, World

MAIL_FOLDERS = ("inbox", "sent", "drafts")
MAX_EXTRACTED_TEXT_BYTES = 64 * 1024


class UnsupportedAppError(KeyError):
    """A world asks to materialize an app that has no integration yet."""


class AppIntegration(Protocol):
    app_name: str

    def materialize(self, world: World, target: Path) -> None: ...

    def extract(self, target: Path) -> dict[str, Any]: ...

    def reset(self, target: Path) -> None: ...


def write_message(message: MailMessage, folder_directory: Path) -> None:
    email_message = EmailMessage()
    email_message["Message-ID"] = f"<{message.id}@rlworld.example>"
    email_message["X-Thread-Id"] = message.thread_id
    email_message["From"] = message.sender
    email_message["To"] = ", ".join(message.recipients)
    email_message["Subject"] = message.subject
    email_message["Date"] = format_datetime(message.sent_at)
    email_message.set_content(message.body)
    folder_directory.mkdir(parents=True, exist_ok=True)
    (folder_directory / f"{message.id}.eml").write_bytes(email_message.as_bytes())


def read_message(message_path: Path) -> dict[str, Any]:
    email_message = BytesParser(policy=policy.default).parsebytes(message_path.read_bytes())
    return {
        "id": message_path.stem,
        "thread_id": str(email_message["X-Thread-Id"] or ""),
        "from": str(email_message["From"]),
        "to": [address.strip() for address in str(email_message["To"]).split(",") if address.strip()],
        "subject": str(email_message["Subject"]),
        "body": email_message.get_content().rstrip("\n"),
        "date": parsedate_to_datetime(str(email_message["Date"])).isoformat(),
    }


class MailIntegration:
    """Maildir style tree: mail/<address>/<folder>/<message id>.eml; extract reads the agent persona's mailbox."""

    app_name = "mail"

    def mail_root(self, target: Path) -> Path:
        return target / "mail"

    def materialize(self, world: World, target: Path) -> None:
        mail_root = self.mail_root(target)
        mail_root.mkdir(parents=True, exist_ok=True)
        (mail_root / "owner.txt").write_text(world.agent_persona.email)
        for message in world.emails:
            if message.is_draft:
                write_message(message, mail_root / message.sender / "drafts")
                continue
            write_message(message, mail_root / message.sender / "sent")
            for recipient in message.recipients:
                write_message(message, mail_root / recipient / "inbox")

    def deliver(self, message: MailMessage, target: Path) -> None:
        """A scripted world event: the message lands in each recipient's inbox mid task."""
        for recipient in message.recipients:
            write_message(message, self.mail_root(target) / recipient / "inbox")

    def extract(self, target: Path) -> dict[str, Any]:
        mail_root = self.mail_root(target)
        owner_path = mail_root / "owner.txt"
        if not owner_path.is_file():
            return {folder: [] for folder in MAIL_FOLDERS}
        mailbox = mail_root / owner_path.read_text().strip()
        return {
            folder: [read_message(message_path) for message_path in sorted((mailbox / folder).glob("*.eml"))]
            for folder in MAIL_FOLDERS
        }

    def reset(self, target: Path) -> None:
        shutil.rmtree(self.mail_root(target), ignore_errors=True)


def describe_file(file_path: Path) -> dict[str, Any]:
    content = file_path.read_bytes()
    description: dict[str, Any] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    if file_path.suffix == ".csv":
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


DEFAULT_INTEGRATIONS: dict[str, AppIntegration] = {
    MailIntegration.app_name: MailIntegration(),
    FilesIntegration.app_name: FilesIntegration(),
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


def reset_world(app_names: Sequence[str], target: Path, integrations: Mapping[str, AppIntegration] | None = None) -> None:
    for integration in resolve_integrations(app_names, integrations or DEFAULT_INTEGRATIONS):
        integration.reset(target)
