"""Wire protocol between the node agent and the guest agent (DESIGN.md sections 6.1 and 6.3).

One JSON object per line each way over a vsock stream. A request carries an id,
a method and params; its response carries the same id and either a result or
an error with a code and a message.
"""

import json
from typing import Any, BinaryIO

MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class GuestFailure(Exception):
    """A request the guest could not serve; the code tells the host why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """The next message, or None once the peer has closed the stream."""
    line = stream.readline(MAX_MESSAGE_BYTES)
    if not line:
        return None
    if not line.endswith(b"\n"):
        raise GuestFailure("message_too_large", f"a message may not exceed {MAX_MESSAGE_BYTES} bytes")
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise GuestFailure("bad_request", "a message must be a JSON object")
    return payload


def write_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8") + b"\n")
    stream.flush()


def result_message(request_id: Any, result: Any) -> dict[str, Any]:
    return {"id": request_id, "result": result}


def error_message(request_id: Any, code: str, message: str) -> dict[str, Any]:
    return {"id": request_id, "error": {"code": code, "message": message}}
