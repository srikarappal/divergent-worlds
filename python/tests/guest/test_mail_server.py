import mailbox
import smtplib
import socket
import sys
import threading
from email.message import EmailMessage

import pytest

from rlworld.guest.mail_server import ImapSession, MailServer, SmtpSession, command_address, deliver, file_sent
from rlworld.worlds.integrations import maildir_folder

FAKE_IMAP = ("import sys\nsys.stdout.write('* PREAUTH ready\\r\\n')\nsys.stdout.flush()\n"
             "sys.stdout.write(sys.stdin.readline())\nsys.stdout.flush()\n")


@pytest.fixture
def mail_root(tmp_path):
    for address in ("cfo@fjord.example", "agent@fjord.example"):
        maildir_folder(tmp_path / "mail" / address / "inbox")
    (tmp_path / "mail" / "current").symlink_to("agent@fjord.example")
    return tmp_path / "mail"


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def inbox(mail_root, address):
    return mailbox.Maildir(mail_root / address / "inbox", factory=None, create=False)


def sent(mail_root, address):
    return mailbox.Maildir(mail_root / address / "sent", factory=None, create=False)


def test_addresses_come_from_smtp_commands():
    assert command_address("FROM:<Agent@Fjord.example> SIZE=120") == "agent@fjord.example"
    assert command_address("TO: cfo@fjord.example") == "cfo@fjord.example"


def test_delivery_reaches_world_mailboxes_and_nobody_outside_the_world(mail_root):
    delivered = deliver(b"Subject: hi\r\n\r\nhello\r\n", ["cfo@fjord.example", "someone@outside.example", "current"],
                        mail_root)
    assert delivered == ["cfo@fjord.example"]
    assert [message.get_subdir() for message in inbox(mail_root, "cfo@fjord.example")] == ["new"]
    assert not (mail_root / "someone@outside.example").exists()


def test_mail_clients_send_through_smtp_to_colleagues_inboxes(mail_root):
    server = MailServer(("127.0.0.1", 0), SmtpSession, mail_root)
    port = serve(server)
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "agent@fjord.example", "cfo@fjord.example", "Totals"
    message.set_content(".leading dot survives\nTotals attached.")
    with smtplib.SMTP("127.0.0.1", port) as client:
        refused = client.send_message(message, to_addrs=["cfo@fjord.example", "vendor@outside.example"])
    server.shutdown()
    assert refused == {}
    delivered = next(iter(inbox(mail_root, "cfo@fjord.example")))
    assert delivered["Subject"] == "Totals" and ".leading dot survives" in delivered.get_payload()
    sent_copies = list(sent(mail_root, "agent@fjord.example"))
    assert [(sent_copy["Subject"], sent_copy.get_subdir(), sent_copy.get_flags())
            for sent_copy in sent_copies] == [("Totals", "cur", "S")]


def test_without_a_current_mailbox_no_sent_copy_is_filed(tmp_path):
    file_sent(b"Subject: hi\r\n\r\nhello\r\n", tmp_path / "mail")
    assert not (tmp_path / "mail").exists()


def test_imap_connections_go_to_the_imap_process_as_they_are(mail_root):
    server = MailServer(("127.0.0.1", 0), ImapSession, mail_root, [sys.executable, "-c", FAKE_IMAP])
    port = serve(server)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        stream = connection.makefile("rwb")
        assert stream.readline() == b"* PREAUTH ready\r\n"
        stream.write(b"a1 LIST \"\" \"*\"\r\n")
        stream.flush()
        assert stream.readline() == b"a1 LIST \"\" \"*\"\r\n"
    server.shutdown()
