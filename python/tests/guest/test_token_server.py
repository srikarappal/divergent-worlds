import json
import socket
import threading
from datetime import datetime, timezone

import pytest

from rlworld.guest import TokenServer
from rlworld.guest.options import parse_arguments
from rlworld.guest.system import SharedKernelHygiene


class HealthHandler:
    def __init__(self):
        self.methods = []

    def handle(self, method, params):
        self.methods.append(method)
        return {"ok": True}


def serve_in_thread(server, connection):
    thread = threading.Thread(target=server.serve, args=(connection,), daemon=True)
    thread.start()
    return thread


def test_clients_with_the_token_reach_the_agent():
    handler = HealthHandler()
    server = TokenServer(handler, "s3cret")
    host_side, guest_side = socket.socketpair()
    thread = serve_in_thread(server, guest_side)
    with host_side, host_side.makefile("rwb") as stream:
        stream.write(b"AUTH s3cret\n")
        stream.flush()
        assert stream.readline() == b"OK\n"
        stream.write(json.dumps({"id": 7, "method": "health", "params": {}}).encode() + b"\n")
        stream.flush()
        assert json.loads(stream.readline()) == {"id": 7, "result": {"ok": True}}
    thread.join(timeout=5)
    assert handler.methods == ["health"]


@pytest.mark.parametrize("greeting", [b"AUTH wrong\n", b"AUTH s3cret extra\n", b"CONNECT 5005\n", b"AUTH s3cret"])
def test_clients_without_the_token_get_nothing(greeting):
    handler = HealthHandler()
    host_side, guest_side = socket.socketpair()
    thread = serve_in_thread(TokenServer(handler, "s3cret"), guest_side)
    with host_side:
        host_side.sendall(greeting)
        host_side.shutdown(socket.SHUT_WR)
        assert host_side.makefile("rb").read() == b""
    thread.join(timeout=5)
    assert handler.methods == []


def test_token_servers_need_a_token():
    with pytest.raises(ValueError, match="needs a token"):
        TokenServer(HealthHandler(), "")


def test_containers_leave_the_shared_kernel_alone():
    hygiene = SharedKernelHygiene()
    hygiene.set_clock(datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc))
    hygiene.add_entropy(b"\x00" * 64)


def test_tcp_transport_needs_a_token_file(capsys):
    arguments = parse_arguments(["--transport", "tcp", "--token-file", "/run/rlworld/agent.token", "--container",
                                 "--launcher", "Calc=libreoffice --calc", "--screen", "1920x1080"])
    assert arguments.transport == "tcp" and arguments.container and arguments.screen == (1920, 1080)
    assert arguments.launcher == [("Calc", "libreoffice --calc")]
    assert parse_arguments([]).transport == "vsock" and not parse_arguments([]).container
    with pytest.raises(SystemExit):
        parse_arguments(["--transport", "tcp"])
    assert "--transport tcp needs --token-file" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        parse_arguments(["--launcher", "no equals sign"])
