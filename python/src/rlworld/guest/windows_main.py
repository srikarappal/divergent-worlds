"""Entry point of the Windows in session helper (DESIGN.md section 6.3); the autologon session's logon script runs it.

It answers the host over its virtio serial port as the session's user: UIA
trees, Excel through COM, the policy's PowerShell, the browser through
DevTools, injection and extraction. Screens, input and the clock belong to the
hypervisor.
"""

from pathlib import Path

from rlworld.guest.agent import GuestAgent
from rlworld.guest.cdp import CdpBrowser
from rlworld.guest.excel import ExcelIntegration, ExcelNative
from rlworld.guest.server import SerialPortServer
from rlworld.guest.windows import COM_ERRORS, UiaAccessibility, make_dpi_aware, running_excel, screen_bounds
from rlworld.guest.windows_options import parse_arguments
from rlworld.guest.windows_system import HypervisorHygiene, HypervisorIO, WindowsShell, launcher_commands
from rlworld.worlds.integrations import DEFAULT_INTEGRATIONS, USER_WORLD_FOLDER


def main() -> None:
    arguments = parse_arguments()
    make_dpi_aware()
    home = Path.home()
    hypervisor = HypervisorIO()
    excel = ExcelIntegration(running_excel, home)
    agent = GuestAgent(
        screen=hypervisor,
        input_runner=hypervisor,
        accessibility=UiaAccessibility(),
        browser=CdpBrowser(arguments.cdp_endpoint),
        shell=WindowsShell(),
        hygiene=HypervisorHygiene(),
        home=home,
        world_root=arguments.world_root or home / USER_WORLD_FOLDER,
        license_root=arguments.license_root,
        secrets_path=arguments.secrets_path,
        launchers=launcher_commands(arguments.launchers),
        viewport=screen_bounds(),
        native={excel.app_name: ExcelNative(running_excel, COM_ERRORS)},
        integrations={**DEFAULT_INTEGRATIONS, excel.app_name: excel},
    )
    SerialPortServer(agent, arguments.port).serve_forever()


if __name__ == "__main__":
    main()
