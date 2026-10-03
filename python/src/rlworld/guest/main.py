"""Entry point of the guest agent inside a Linux image (DESIGN.md section 6.3); the image runs it as root."""

import argparse
import pwd
from pathlib import Path

from rlworld.actions.accessibility import Bounds
from rlworld.guest.agent import GuestAgent
from rlworld.guest.atspi import AtspiAccessibility
from rlworld.guest.cdp import CdpBrowser
from rlworld.guest.server import DEFAULT_PORT, VsockServer
from rlworld.guest.system import DisplayCommandRunner, SystemHygiene, UserShell, X11Screen


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
    parser = argparse.ArgumentParser(prog="rlworld-guest-agent", description="Serve the host over vsock.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--display", default=":0")
    parser.add_argument("--user", default="agent")
    parser.add_argument("--world-root", type=Path, default=Path("/srv/world"))
    parser.add_argument("--license-root", type=Path, default=Path("/etc/rlworld/licenses"))
    parser.add_argument("--secrets-path", type=Path, default=Path("/run/rlworld/episode.env"))
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--screen", type=screen_size, default=(1280, 800))
    parser.add_argument("--launcher", type=launcher, action="append", default=[])
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    account = pwd.getpwnam(arguments.user)
    width, height = arguments.screen
    agent = GuestAgent(
        screen=X11Screen(arguments.display),
        input_runner=DisplayCommandRunner(arguments.display),
        accessibility=AtspiAccessibility(),
        browser=CdpBrowser(arguments.cdp_endpoint),
        shell=UserShell(arguments.user, account.pw_dir, arguments.display),
        hygiene=SystemHygiene(),
        home=Path(account.pw_dir),
        world_root=arguments.world_root,
        license_root=arguments.license_root,
        secrets_path=arguments.secrets_path,
        launchers=dict(arguments.launcher),
        viewport=Bounds(x=0, y=0, width=width, height=height),
        owner=(account.pw_uid, account.pw_gid),
    )
    VsockServer(agent, arguments.port).serve_forever()


if __name__ == "__main__":
    main()
