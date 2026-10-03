"""The guest agent's servers (DESIGN.md section 6.3).

Only the host may connect: a vsock peer other than the host CID is refused, so
nothing inside the guest can drive its own agent. Development containers have
no vsock (docs/TASKS.md build decision 4), so there the agent listens on TCP and
serves only a client whose first line carries the container's token, kept in a
root only file the policy's shell cannot read. Each connection is served on its
own thread, one request at a time; requests from all connections run one after
another because the screen and input are shared.
"""

import hmac
import socket
import threading
from typing import Any, BinaryIO, Protocol

from rlworld.guest.protocol import GuestFailure, error_message, read_message, result_message, write_message

VMADDR_CID_HOST = 2
DEFAULT_PORT = 5005
MAX_GREETING_BYTES = 512


class RequestHandler(Protocol):
    def handle(self, method: str, params: dict[str, Any]) -> Any: ...


def serve_stream(stream: BinaryIO, handler: RequestHandler, lock: threading.Lock) -> None:
    """Answers requests until the host closes the stream; a failing backend answers with an error."""
    while True:
        try:
            message = read_message(stream)
        except (GuestFailure, ValueError) as error:
            write_message(stream, error_message(None, "bad_request", str(error)))
            return
        if message is None:
            return
        request_id = message.get("id")
        params = message.get("params") or {}
        try:
            with lock:
                result = handler.handle(str(message.get("method", "")), params)
        except GuestFailure as error:
            write_message(stream, error_message(request_id, error.code, str(error)))
        except Exception as error:  # a broken backend answers with an error instead of killing the agent
            write_message(stream, error_message(request_id, "internal", f"{type(error).__name__}: {error}"))
        else:
            write_message(stream, result_message(request_id, result))


class VsockServer:
    def __init__(self, handler: RequestHandler, port: int = DEFAULT_PORT, allowed_cid: int = VMADDR_CID_HOST) -> None:
        self.handler = handler
        self.port = port
        self.allowed_cid = allowed_cid
        self.lock = threading.Lock()

    def accepts(self, peer_cid: int) -> bool:
        return peer_cid == self.allowed_cid

    def serve(self, connection: socket.socket) -> None:
        with connection, connection.makefile("rwb") as stream:
            serve_stream(stream, self.handler, self.lock)

    def serve_forever(self) -> None:
        listener = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        listener.bind((socket.VMADDR_CID_ANY, self.port))
        listener.listen()
        while True:
            connection, (peer_cid, _) = listener.accept()
            if not self.accepts(peer_cid):
                connection.close()
                continue
            threading.Thread(target=self.serve, args=(connection,), daemon=True).start()


class TokenServer:
    def __init__(self, handler: RequestHandler, token: str, port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> None:
        if not token:
            raise ValueError("a token server needs a token")
        self.handler = handler
        self.expected_greeting = f"AUTH {token}\n".encode("utf-8")
        self.port = port
        self.host = host
        self.lock = threading.Lock()

    def authenticated(self, stream: BinaryIO) -> bool:
        return hmac.compare_digest(stream.readline(MAX_GREETING_BYTES), self.expected_greeting)

    def serve(self, connection: socket.socket) -> None:
        """Clients without the token get no answer at all, then the connection closes."""
        with connection, connection.makefile("rwb") as stream:
            if not self.authenticated(stream):
                return
            stream.write(b"OK\n")
            stream.flush()
            serve_stream(stream, self.handler, self.lock)

    def serve_forever(self) -> None:
        listener = socket.create_server((self.host, self.port))
        while True:
            connection, _ = listener.accept()
            threading.Thread(target=self.serve, args=(connection,), daemon=True).start()
