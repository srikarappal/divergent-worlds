"""Command line options of the Windows in session helper, apart from its COM backends so they parse anywhere."""

import argparse
from collections.abc import Sequence
from pathlib import Path

# The helper's virtio serial port, named by the QEMU driver's virtserialport device.
HELPER_PORT = r"\\.\Global\org.rlworld.helper.0"


def build_parser() -> argparse.ArgumentParser:
    local_state = Path.home() / "AppData" / "Local" / "rlworld"
    parser = argparse.ArgumentParser(prog="rlworld-windows-helper",
                                     description="Serve the host over the helper's virtio serial port.")
    parser.add_argument("--launchers", type=Path, required=True, help="the image's launchers file")
    parser.add_argument("--port", default=HELPER_PORT, help="the helper's virtio serial port")
    parser.add_argument("--world-root", type=Path, default=None,
                        help="where the world is materialized; the user's home world folder by default")
    parser.add_argument("--license-root", type=Path, default=local_state / "licenses")
    parser.add_argument("--secrets-path", type=Path, default=local_state / "episode.env")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    return parser


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)
