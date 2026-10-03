"""How each app in an image spec gets installed and launched (DESIGN.md sections 6.2 and 6.4).

An app's version label is part of the pin: downloads are looked up in the
artifact lock as <app>_<label>, apt packages resolve against the dated snapshot
mirror and Office labels map to their volume licensed product and channel.
Every app names the window it opens, which the ready probe and the guest
agent's launchers use. Browsers expose DevTools on loopback only and their
page content to the accessibility tree.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from rlworld.images.config import BuildError, WindowsBuildConfig

DOWNLOAD_PLACEHOLDER = "{download}"
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9._+:~-]+$")
CHROMIUM_BASE_FLAGS = ("--remote-debugging-port=9222", "--remote-allow-origins=http://127.0.0.1:9222",
                       "--force-renderer-accessibility", "--start-maximized")
OFFICE_PRODUCTS = {
    "office_ltsc_2024": ("Excel2024Volume", "PerpetualVL2024"),
    "office_ltsc_2021": ("Excel2021Volume", "PerpetualVL2021"),
}


@dataclass(frozen=True)
class LinuxApp:
    """Install lines run as root in the image build; {download} is the checked download."""

    window: str
    launch: str
    packages: tuple[str, ...] = ()
    artifact: str | None = None
    install: tuple[str, ...] = ()


@dataclass(frozen=True)
class WindowsApp:
    """Install lines are PowerShell run by the build; {download} is the checked download."""

    window: str
    executable: str
    arguments: tuple[str, ...]
    artifact: str
    install: tuple[str, ...]
    office_product: tuple[str, str] | None = None


def chromium_launch(binary: str, profile_directory: str, flags: Sequence[str]) -> str:
    return " ".join((binary, *CHROMIUM_BASE_FLAGS, *flags, f"--user-data-dir={profile_directory}"))


def check_label(app: str, label: str) -> None:
    """Labels end up in shell and PowerShell lines, so they hold version characters only."""
    if not LABEL_PATTERN.match(label):
        raise BuildError([f"version label {label!r} of app {app} may only hold letters, digits and ._+:~-"])


def linux_app(app: str, label: str, user: str, chromium_flags: Sequence[str] = ()) -> LinuxApp:
    check_label(app, label)
    if app == "chrome":
        return LinuxApp(
            window="Chrome", artifact=f"chrome_{label}",
            launch=chromium_launch("/opt/chrome/chrome", f"/home/{user}/.config/rlworld-chrome", chromium_flags),
            install=("unzip -q {download} -d /opt", "mv /opt/chrome-linux64 /opt/chrome",
                     r"apt-get install -y --no-install-recommends $(sed -E 's/ *\([^)]*\)//g; s/ *\|.*//'"
                     " /opt/chrome/deb.deps)"))
    if app == "firefox":
        return LinuxApp(window="Firefox", artifact=f"firefox_{label}", launch="/opt/firefox/firefox",
                        packages=("libgtk-3-0", "libasound2", "libdbus-glib-1-2", "libx11-xcb1", "bzip2"),
                        install=("tar -xf {download} -C /opt",))
    if app == "edge":
        return LinuxApp(
            window="Edge", artifact=f"edge_{label}",
            launch=chromium_launch("/usr/bin/microsoft-edge", f"/home/{user}/.config/rlworld-edge", chromium_flags),
            install=("apt-get install -y --no-install-recommends {download}",))
    if app == "libreoffice":
        return LinuxApp(window="Calc", launch="libreoffice --calc --nologo --norestore",
                        packages=(f"libreoffice-calc={label}", f"libreoffice-gtk3={label}"))
    if app == "webkit":
        return LinuxApp(
            window="WebKit", launch="sh -c '/opt/playwright/browsers/webkit-*/minibrowser-gtk/MiniBrowser'",
            install=("python3 -m venv /opt/playwright",
                     f"/opt/playwright/bin/pip install --no-cache-dir playwright=={label}",
                     "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright/browsers /opt/playwright/bin/playwright install"
                     " --with-deps webkit"))
    raise BuildError([f"no Linux recipe for app {app}"])


def windows_app(app: str, label: str, config: WindowsBuildConfig) -> WindowsApp:
    check_label(app, label)
    if app == "excel":
        if label not in OFFICE_PRODUCTS:
            raise BuildError([f"no Office product for excel {label}; known: {', '.join(sorted(OFFICE_PRODUCTS))}"])
        return WindowsApp(
            window="Excel", executable=r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE", arguments=(),
            artifact=config.office_tool_artifact, office_product=OFFICE_PRODUCTS[label],
            install=(r"Start-Process -FilePath '{download}'"
                     r" -ArgumentList '/quiet', '/extract:C:\rlworld\downloads\odt' -Wait",
                     r"$setup = Start-Process -FilePath 'C:\rlworld\downloads\odt\setup.exe'"
                     r" -ArgumentList '/configure', 'C:\rlworld\office.xml' -Wait -PassThru",
                     'if ($setup.ExitCode -ne 0) { throw "Office setup exited with $($setup.ExitCode)" }'))
    if app == "edge":
        return WindowsApp(
            window="Edge", executable=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            arguments=("--start-maximized",), artifact=f"edge_{label}",
            install=("$msi = Start-Process -FilePath msiexec.exe -ArgumentList '/i', '{download}', '/qn', '/norestart'"
                     " -Wait -PassThru",
                     'if ($msi.ExitCode -notin 0, 3010) { throw "Edge setup exited with $($msi.ExitCode)" }'))
    raise BuildError([f"no Windows recipe for app {app}"])
