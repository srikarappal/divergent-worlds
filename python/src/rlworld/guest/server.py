"""The guest agent's vsock server (DESIGN.md section 6.3).

Only the host may connect: a vsock peer other than the host CID is refused, so
nothing inside the guest can drive its own agent. Each connection is served on
its own thread, one request at a time; requests from all connections run
one after another because the screen and input are shared.
"""

import socket
import threading
from typing import Any, BinaryIO, Protocol

from rlworld.guest.protocol import GuestFailure, error_message, read_message, result_message, write_message

VMADDR_CID_HOST = 2
DEFAULT_PORT = 5005


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
