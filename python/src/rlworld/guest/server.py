"""The guest agent's servers (DESIGN.md section 6.3).

Only the host may connect: a vsock peer other than the host CID is refused, so
nothing inside the guest can drive its own agent. Development containers have
no vsock (docs/TASKS.md build decision 4), so there the agent listens on TCP and
serves only a client whose first line carries the container's token, kept in a
root only file the policy's shell cannot read. Each connection is served on its
own thread, one request at a time; requests from all connections run one after
another because the screen and input are shared. For operator takeover the same
gates lead to the VNC server on the display, which listens only on a root only
Unix socket: vsock port 5901 from the host, or a VNC greeting with the token.
"""

import hmac
import socket
import threading
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from rlworld.guest.protocol import GuestFailure, error_message, read_message, result_message, write_message

VMADDR_CID_HOST = 2
DEFAULT_PORT = 5005
DEFAULT_VNC_PORT = 5901
DEFAULT_VNC_SOCKET = Path("/run/rlworld/vnc.sock")
MAX_GREETING_BYTES = 512
RELAY_CHUNK_BYTES = 65_536


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


def shutdown_writes(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_WR)
    except OSError:
        return


def pump(source: socket.socket, destination: socket.socket) -> None:
    """Copies one direction until it ends, then tells the other side no more bytes are coming."""
    try:
        while chunk := source.recv(RELAY_CHUNK_BYTES):
            destination.sendall(chunk)
    except OSError:
        return
    finally:
        shutdown_writes(destination)


def relay(client: socket.socket, vnc_socket: Path) -> None:
    """Joins the host's connection to the VNC server in both directions until both directions end."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as vnc:
        vnc.connect(str(vnc_socket))
        to_client = threading.Thread(target=pump, args=(vnc, client), daemon=True)
        to_client.start()
        pump(client, vnc)
        to_client.join()


class VncRelayServer:
    """vsock port 5901 from the host CID only, joined to the VNC server's Unix socket."""

    def __init__(self, vnc_socket: Path = DEFAULT_VNC_SOCKET, port: int = DEFAULT_VNC_PORT,
                 allowed_cid: int = VMADDR_CID_HOST) -> None:
        self.vnc_socket = vnc_socket
        self.port = port
        self.allowed_cid = allowed_cid

    def serve(self, connection: socket.socket) -> None:
        with connection:
            relay(connection, self.vnc_socket)

    def serve_forever(self) -> None:
        listener = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        listener.bind((socket.VMADDR_CID_ANY, self.port))
        listener.listen()
        while True:
            connection, (peer_cid, _) = listener.accept()
            if peer_cid != self.allowed_cid:
                connection.close()
                continue
            threading.Thread(target=self.serve, args=(connection,), daemon=True).start()


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
    def __init__(self, handler: RequestHandler, token: str, port: int = DEFAULT_PORT, host: str = "0.0.0.0",
                 vnc_socket: Path | None = None) -> None:
        if not token:
            raise ValueError("a token server needs a token")
        self.handler = handler
        self.greetings = {kind: f"{kind} {token}\n".encode("utf-8") for kind in ("AUTH", "VNC")}
        self.port = port
        self.host = host
        self.vnc_socket = vnc_socket
        self.lock = threading.Lock()

    def greeting(self, stream: BinaryIO) -> str | None:
        """AUTH asks for the agent and VNC for the screen; anything without the token is nothing."""
        line = stream.readline(MAX_GREETING_BYTES)
        for kind, expected in self.greetings.items():
            if hmac.compare_digest(line, expected):
                return kind
        return None

    def serve(self, connection: socket.socket) -> None:
        """Clients without the token get no answer at all, then the connection closes."""
        with connection, connection.makefile("rwb") as stream:
            kind = self.greeting(stream)
            if kind is None or (kind == "VNC" and self.vnc_socket is None):
                return
            stream.write(b"OK\n")
            stream.flush()
            if kind == "AUTH":
                serve_stream(stream, self.handler, self.lock)
            else:
                relay(connection, self.vnc_socket)

    def serve_forever(self) -> None:
        listener = socket.create_server((self.host, self.port))
        while True:
            connection, _ = listener.accept()
            threading.Thread(target=self.serve, args=(connection,), daemon=True).start()
