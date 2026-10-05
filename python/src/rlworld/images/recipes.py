"""How each app in an image spec gets installed and launched (DESIGN.md sections 6.2 and 6.4).

An app's version label is part of the pin: downloads are looked up in the
artifact lock as <app>_<label>, apt packages resolve against the dated snapshot
mirror and Office labels map to their volume licensed product and channel.
Every app names the window it opens, which the ready probe and the guest
agent's launchers use. Browsers expose DevTools on loopback only and their
page content to the accessibility tree. Apps installed outside apt bring a
menu entry; apps may also bring files and services the desktop user runs.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from rlworld.guest.chat_app import DEFAULT_CHAT_PORT
from rlworld.images.config import BuildError, WindowsBuildConfig
from rlworld.images.mail_client import THUNDERBIRD_ICON, THUNDERBIRD_LAUNCHER, mail_client_files

DOWNLOAD_PLACEHOLDER = "{download}"
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9._+:~-]+$")
CHROMIUM_BASE_FLAGS = ("--remote-debugging-port=9222", "--remote-allow-origins=http://127.0.0.1:9222",
                       "--force-renderer-accessibility", "--start-maximized")
FIREFOX_ICON = "/opt/firefox/browser/chrome/icons/default/default128.png"
GUEST_BIN = "/opt/rlworld/bin"
CHAT_URL = f"http://127.0.0.1:{DEFAULT_CHAT_PORT}/"
# Runtime libraries of Mozilla's Linux builds, which ship everything else in their own folder; Ubuntu 24.04 names
# the 64 bit time builds of GTK and ALSA with a t64 suffix.
MOZILLA_PACKAGES = ("libgtk-3-0t64", "libasound2t64", "libx11-xcb1", "bzip2")
# LibreOffice modules beside Calc, which the libreoffice app installs, by app name and the window each opens.
LIBREOFFICE_MODULES = {"libreoffice_writer": "Writer", "libreoffice_impress": "Impress"}
OFFICE_PRODUCTS = {
    "office_ltsc_2024": ("Excel2024Volume", "PerpetualVL2024"),
    "office_ltsc_2021": ("Excel2021Volume", "PerpetualVL2021"),
}


@dataclass(frozen=True)
class MenuEntry:
    """A desktop menu entry for an app that apt did not install."""

    name: str
    command: str
    icon: str
    categories: str


@dataclass(frozen=True)
class LinuxService:
    """A long running process the desktop user runs from the desktop user's home, such as a local server."""

    name: str
    command: str


@dataclass(frozen=True)
class LinuxApp:
    """Install lines run as root in the image build; {download} is the checked download. Files land with the root
    overlay before anything is installed."""

    window: str
    launch: str
    packages: tuple[str, ...] = ()
    artifact: str | None = None
    install: tuple[str, ...] = ()
    menu: MenuEntry | None = None
    files: Mapping[str, str] = field(default_factory=dict)
    services: tuple[LinuxService, ...] = ()


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
                        packages=(*MOZILLA_PACKAGES, "libdbus-glib-1-2"),
                        install=("tar -xf {download} -C /opt", "ln -sf /opt/firefox/firefox /usr/local/bin/firefox"),
                        menu=MenuEntry("Firefox Web Browser", "firefox %u", FIREFOX_ICON, "Network;WebBrowser;"))
    if app == "thunderbird":
        return LinuxApp(window="Thunderbird", artifact=f"thunderbird_{label}", launch=THUNDERBIRD_LAUNCHER,
                        packages=(*MOZILLA_PACKAGES, "dovecot-imapd"),
                        install=("tar -xf {download} -C /opt",
                                 f"ln -sf {THUNDERBIRD_LAUNCHER} /usr/local/bin/thunderbird",
                                 "systemctl mask dovecot.service dovecot.socket"),
                        menu=MenuEntry("Thunderbird Mail", "thunderbird %u", THUNDERBIRD_ICON, "Network;Email;"),
                        files=mail_client_files(user),
                        services=(LinuxService("mail", f"{GUEST_BIN}/rlworld-mail-server"),))
    if app == "chat":
        return LinuxApp(window="Chat", launch=f"/opt/firefox/firefox --new-window {CHAT_URL}",
                        menu=MenuEntry("Chat", f"firefox --new-window {CHAT_URL}", "user-available", "Network;Chat;"),
                        services=(LinuxService("chat", f"{GUEST_BIN}/rlworld-chat-app"),))
    if app == "edge":
        return LinuxApp(
            window="Edge", artifact=f"edge_{label}",
            launch=chromium_launch("/usr/bin/microsoft-edge", f"/home/{user}/.config/rlworld-edge", chromium_flags),
            install=("apt-get install -y --no-install-recommends {download}",))
    if app == "libreoffice":
        return LinuxApp(window="Calc", launch="libreoffice --calc --nologo --norestore",
                        packages=(f"libreoffice-calc={label}", f"libreoffice-gtk3={label}"))
    if app in LIBREOFFICE_MODULES:
        window = LIBREOFFICE_MODULES[app]
        return LinuxApp(window=window, launch=f"libreoffice --{window.lower()} --nologo --norestore",
                        packages=(f"libreoffice-{window.lower()}={label}", f"libreoffice-gtk3={label}"))
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
