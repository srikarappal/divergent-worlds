"""Command line options of the guest agent, apart from its system backends so they parse anywhere."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from rlworld.guest.server import DEFAULT_PORT, DEFAULT_VNC_PORT, DEFAULT_VNC_SOCKET


def launcher(text: str) -> tuple[str, str]:
    name, separator, command = text.partition("=")
    if not separator or not name or not command:
        raise argparse.ArgumentTypeError(f"a launcher looks like Excel=command, got {text!r}")
    return name, command


def screen_size(text: str) -> tuple[int, int]:
    width, separator, height = text.partition("x")
    if not separator or not width.isdigit() or not height.isdigit():
        raise argparse.ArgumentTypeError(f"a screen size looks like 1280x800, got {text!r}")
    return int(width), int(height)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rlworld-guest-agent", description="Serve the host over vsock or tcp.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--display", default=":0")
    parser.add_argument("--user", default="agent")
    parser.add_argument("--world-root", type=Path, default=Path("/srv/world"))
    parser.add_argument("--license-root", type=Path, default=Path("/etc/rlworld/licenses"))
    parser.add_argument("--secrets-path", type=Path, default=Path("/run/rlworld/episode.env"))
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--screen", type=screen_size, default=(1280, 800))
    parser.add_argument("--launcher", type=launcher, action="append", default=[])
    parser.add_argument("--transport", choices=("vsock", "tcp"), default="vsock")
    parser.add_argument("--token-file", type=Path, help="root only file holding the token tcp clients must present")
    parser.add_argument("--container", action="store_true", help="share the host kernel: leave the clock and RNG alone")
    parser.add_argument("--vnc-socket", type=Path, default=DEFAULT_VNC_SOCKET, help="the VNC server's Unix socket")
    parser.add_argument("--vnc-port", type=int, default=DEFAULT_VNC_PORT, help="vsock port for operator takeover")
    return parser


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.transport == "tcp" and arguments.token_file is None:
        parser.error("--transport tcp needs --token-file")
    return arguments
