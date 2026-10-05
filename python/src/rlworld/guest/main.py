"""Entry point of the guest agent inside a Linux image (DESIGN.md section 6.3); the image runs it as root.

VMs serve the host over vsock. Development containers (docs/TASKS.md build
decision 4) serve TCP behind the container's token and leave the shared kernel's
clock and RNG alone.
"""

import pwd
import threading
from pathlib import Path

from rlworld.actions.accessibility import Bounds
from rlworld.guest.agent import GuestAgent
from rlworld.guest.atspi import AtspiAccessibility
from rlworld.guest.cdp import CdpBrowser
from rlworld.guest.options import parse_arguments
from rlworld.guest.server import TokenServer, VncRelayServer, VsockServer
from rlworld.guest.system import DisplayCommandRunner, SharedKernelHygiene, SystemHygiene, UserShell, X11Screen
from rlworld.worlds.integrations import USER_WORLD_FOLDER


def main() -> None:
    arguments = parse_arguments()
    account = pwd.getpwnam(arguments.user)
    width, height = arguments.screen
    agent = GuestAgent(
        screen=X11Screen(arguments.display),
        input_runner=DisplayCommandRunner(arguments.display),
        accessibility=AtspiAccessibility(arguments.user, arguments.session_bus, arguments.display),
        browser=CdpBrowser(arguments.cdp_endpoint),
        shell=UserShell(arguments.user, account.pw_dir, arguments.display),
        hygiene=SharedKernelHygiene() if arguments.container else SystemHygiene(),
        home=Path(account.pw_dir),
        world_root=arguments.world_root or Path(account.pw_dir) / USER_WORLD_FOLDER,
        license_root=arguments.license_root,
        secrets_path=arguments.secrets_path,
        launchers=dict(arguments.launcher),
        viewport=Bounds(x=0, y=0, width=width, height=height),
        owner=(account.pw_uid, account.pw_gid),
    )
    if arguments.transport == "tcp":
        TokenServer(agent, arguments.token_file.read_text().strip(), arguments.port,
                    vnc_socket=arguments.vnc_socket).serve_forever()
    else:
        vnc_relay = VncRelayServer(arguments.vnc_socket, arguments.vnc_port)
        threading.Thread(target=vnc_relay.serve_forever, daemon=True).start()
        VsockServer(agent, arguments.port).serve_forever()


if __name__ == "__main__":
    main()
