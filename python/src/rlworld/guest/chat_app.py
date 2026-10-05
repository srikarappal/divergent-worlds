"""The world's chat as a web app in the guest (DESIGN.md sections 6.3 and 6.4).

The desktop user runs this server and opens its page in the browser: channels
and direct messages from the world's chat folder, with posts written back to
that folder as the agent persona. The page reads the folder again every two
seconds, so colleague replies an episode delivers show up on their own.
"""

import argparse
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any

from rlworld.worlds.integrations import PEOPLE_FILE, USER_WORLD_FOLDER, ChatIntegration, direct_channel, utc_time
from rlworld.worlds.model import ChatMessage

DEFAULT_CHAT_PORT = 8065
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_CHARACTERS = 4000
CHAT = ChatIntegration()


class ChatError(ValueError):
    """A post the app refuses, such as one to a channel that does not exist."""


def read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.is_file() else default


def channel_title(channel: str, people: dict[str, dict[str, str]]) -> str:
    if channel.startswith("dm_"):
        person_id = channel.removeprefix("dm_")
        return people.get(person_id, {}).get("name", person_id)
    return f"#{channel}"


def channel_sort_key(people: dict[str, dict[str, str]], channel: str) -> tuple[bool, str]:
    """Channels first, then direct messages, each by title."""
    return channel.startswith("dm_"), channel_title(channel, people).lower()


def chat_state(world_root: Path) -> dict[str, Any]:
    """Every channel with its messages in order, direct messages to every colleague included even when empty."""
    chat_root = CHAT.chat_root(world_root)
    owner = (chat_root / "owner.txt").read_text().strip() if (chat_root / "owner.txt").is_file() else ""
    people = read_json(chat_root / PEOPLE_FILE, {})
    channels: dict[str, list[dict[str, str]]] = {direct_channel(person_id): [] for person_id in people
                                                 if person_id != owner}
    if chat_root.is_dir():
        channels.update({directory.name: [] for directory in chat_root.iterdir() if directory.is_dir()})
    for message in CHAT.messages(world_root):
        channels.setdefault(message.channel, []).append({
            "id": message.id, "sender_id": message.sender_id, "text": message.text,
            "sender": people.get(message.sender_id, {}).get("name", message.sender_id),
            "sent_at": utc_time(message.sent_at).isoformat()})
    ordered = sorted(channels, key=partial(channel_sort_key, people))
    return {"owner": owner, "channels": [{"name": name, "title": channel_title(name, people),
                                          "messages": channels[name]} for name in ordered]}


def post_message(world_root: Path, channel: str, text: str) -> ChatMessage:
    state = chat_state(world_root)
    if channel not in {entry["name"] for entry in state["channels"]}:
        raise ChatError(f"there is no channel {channel}")
    text = text.strip()
    if not text or len(text) > MAX_TEXT_CHARACTERS:
        raise ChatError(f"a message holds 1 to {MAX_TEXT_CHARACTERS} characters")
    message = ChatMessage(id=f"post_{uuid.uuid4().hex[:16]}", channel=channel, sender_id=state["owner"], text=text,
                          sent_at=datetime.now(UTC))
    CHAT.write(message, world_root)
    return message


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], world_root: Path) -> None:
        super().__init__(address, ChatRequest)
        self.world_root = world_root


class ChatRequest(BaseHTTPRequestHandler):
    server: ChatServer

    def log_message(self, message_format: str, *args: Any) -> None:
        pass

    def send_body(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: HTTPStatus, payload: Any) -> None:
        self.send_body(status, "application/json", json.dumps(payload).encode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/":
            page = resources.files("rlworld.guest").joinpath("chat_page.html").read_bytes()
            self.send_body(HTTPStatus.OK, "text/html; charset=utf-8", page)
        elif self.path == "/api/state":
            self.send_json(HTTPStatus.OK, chat_state(self.server.world_root))
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if self.path != "/api/messages" or length > MAX_BODY_BYTES:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            message = post_message(self.server.world_root, str(body.get("channel", "")), str(body.get("text", "")))
        except (ChatError, ValueError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self.send_json(HTTPStatus.CREATED, json.loads(message.model_dump_json()))


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rlworld-chat-app", description="Serve the world's chat as a web app.")
    parser.add_argument("--world-root", type=Path, default=Path.home() / USER_WORLD_FOLDER)
    parser.add_argument("--port", type=int, default=DEFAULT_CHAT_PORT)
    return parser.parse_args(argv)


def main() -> None:
    arguments = parse_arguments()
    ChatServer(("127.0.0.1", arguments.port), arguments.world_root).serve_forever()


if __name__ == "__main__":
    main()
