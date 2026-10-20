"""World mail inside the guest (DESIGN.md sections 6.3 and 6.4): SMTP to send and IMAP to read.

The desktop user runs this server. Mail clients send through its SMTP port,
which delivers each message unread to the inbox of every recipient with a world
mailbox, drops mail for addresses outside the world and files a read copy in the
sent folder of the mailbox the world links as current, so mail sent from a script
counts like mail sent from Thunderbird, which keeps no copy of its own. Clients
read through its IMAP port, which hands each connection to Dovecot's imap process
started as the same user; it greets preauthenticated, so clients keep no password,
and serves the current mailbox.
"""

import argparse
import getpass
import mailbox
import re
import socketserver
import subprocess
import threading
from collections.abc import Sequence
from pathlib import Path

from rlworld.worlds.integrations import CURRENT_MAILBOX, USER_WORLD_FOLDER, maildir_folder

DEFAULT_SMTP_PORT = 2525
DEFAULT_IMAP_PORT = 1143
DOVECOT_IMAP = "/usr/lib/dovecot/imap"
DOVECOT_CONFIG = Path("/etc/rlworld/dovecot-imap.conf")
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_LINE_BYTES = 4096
ADDRESS_PATTERN = re.compile(r"<([^<>\s]*)>")


def command_address(argument: str) -> str:
    """The address in MAIL FROM:<a@b> or RCPT TO:<a@b>."""
    match = ADDRESS_PATTERN.search(argument)
    return match.group(1).lower() if match else argument.partition(":")[2].strip().lower()


def deliver(message_bytes: bytes, recipients: Sequence[str], mail_root: Path) -> list[str]:
    """Lands the message unread in the inbox of every recipient with a world mailbox and returns those recipients."""
    delivered = []
    for recipient in recipients:
        recipient_mailbox = mail_root / recipient
        if recipient_mailbox.is_dir() and not recipient_mailbox.is_symlink():
            maildir_folder(recipient_mailbox / "inbox").add(mailbox.MaildirMessage(message_bytes))
            delivered.append(recipient)
    return delivered


def file_sent(message_bytes: bytes, mail_root: Path) -> None:
    """Files a read copy in the current mailbox's sent folder, as servers that keep sent mail for clients do."""
    current_mailbox = mail_root / CURRENT_MAILBOX
    if not current_mailbox.is_dir():
        return
    sent_message = mailbox.MaildirMessage(message_bytes)
    sent_message.set_subdir("cur")
    sent_message.set_flags("S")
    maildir_folder(current_mailbox / "sent").add(sent_message)


class MailServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[socketserver.BaseRequestHandler], mail_root: Path,
                 imap_argv: Sequence[str] = ()) -> None:
        super().__init__(address, handler)
        self.mail_root = mail_root
        self.imap_argv = list(imap_argv)


class SmtpSession(socketserver.StreamRequestHandler):
    """Enough SMTP for mail clients: HELO, EHLO, MAIL, RCPT, DATA, RSET, NOOP and QUIT, without authentication."""

    server: MailServer

    def setup(self) -> None:
        super().setup()
        self.recipients: list[str] = []
        self.has_sender = False

    def reply(self, *lines: str) -> None:
        self.wfile.write("".join(f"{line}\r\n" for line in lines).encode("utf-8"))
        self.wfile.flush()

    def handle(self) -> None:
        self.reply("220 rlworld ESMTP")
        while True:
            line = self.rfile.readline(MAX_LINE_BYTES)
            if not line:
                return
            verb, _, argument = line.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            if verb.upper() == "QUIT":
                self.reply("221 bye")
                return
            handler = getattr(self, f"smtp_{verb.lower()}", None)
            if handler is None:
                self.reply("502 command not implemented")
            else:
                handler(argument)

    def smtp_helo(self, argument: str) -> None:
        self.reply("250 rlworld")

    def smtp_ehlo(self, argument: str) -> None:
        self.reply("250-rlworld", "250-8BITMIME", f"250 SIZE {MAX_MESSAGE_BYTES}")

    def smtp_mail(self, argument: str) -> None:
        self.recipients, self.has_sender = [], True
        self.reply("250 sender ok")

    def smtp_rcpt(self, argument: str) -> None:
        if not self.has_sender:
            self.reply("503 MAIL first")
            return
        self.recipients.append(command_address(argument))
        self.reply("250 recipient ok")

    def smtp_data(self, argument: str) -> None:
        if not self.recipients:
            self.reply("503 RCPT first")
            return
        self.reply("354 end with a line holding a single dot")
        lines = []
        size = 0
        while True:
            line = self.rfile.readline(MAX_LINE_BYTES)
            if not line or line in (b".\r\n", b".\n"):
                break
            size += len(line)
            lines.append(line[1:] if line.startswith(b"..") else line)
        if size > MAX_MESSAGE_BYTES:
            self.reply("552 message too large")
        else:
            message_bytes = b"".join(lines)
            deliver(message_bytes, self.recipients, self.server.mail_root)
            file_sent(message_bytes, self.server.mail_root)
            self.reply("250 queued")
        self.recipients, self.has_sender = [], False

    def smtp_rset(self, argument: str) -> None:
        self.recipients, self.has_sender = [], False
        self.reply("250 reset")

    def smtp_noop(self, argument: str) -> None:
        self.reply("250 ok")


class ImapSession(socketserver.BaseRequestHandler):
    """Hands the connection to Dovecot's imap process, which serves it until the client logs out."""

    server: MailServer

    def handle(self) -> None:
        connection = self.request.fileno()
        environment = {"USER": getpass.getuser(), "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}
        subprocess.run(self.server.imap_argv, stdin=connection, stdout=connection, stderr=subprocess.DEVNULL,
                       env=environment, check=False)


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rlworld-mail-server", description="Serve world mail over SMTP and IMAP.")
    parser.add_argument("--world-root", type=Path, default=Path.home() / USER_WORLD_FOLDER)
    parser.add_argument("--smtp-port", type=int, default=DEFAULT_SMTP_PORT)
    parser.add_argument("--imap-port", type=int, default=DEFAULT_IMAP_PORT)
    parser.add_argument("--dovecot-config", type=Path, default=DOVECOT_CONFIG)
    return parser.parse_args(argv)


def main() -> None:
    arguments = parse_arguments()
    mail_root = arguments.world_root / "mail"
    imap = MailServer(("127.0.0.1", arguments.imap_port), ImapSession, mail_root,
                      [DOVECOT_IMAP, "-c", str(arguments.dovecot_config)])
    threading.Thread(target=imap.serve_forever, daemon=True).start()
    MailServer(("127.0.0.1", arguments.smtp_port), SmtpSession, mail_root).serve_forever()


if __name__ == "__main__":
    main()
