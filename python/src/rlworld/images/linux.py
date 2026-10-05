"""Linux images: a pinned Dockerfile becomes an ext4 rootfs plus a guest kernel (DESIGN.md sections 6.1 to 6.3).

The Dockerfile starts from the spec's base image by digest and installs from
dated snapshot mirrors only; downloads are checked against the artifact lock
and the guest agent's dependencies against their hashes. The image boots
systemd with the clone address behind NAT, Xvfb, openbox and a session bus
whose accessibility bus the root guest agent may read; x11vnc serves the screen
for operator takeover on a root only Unix socket. The agent listens on
vsock and nothing else touches the clock, which the agent sets to world time.
BuildKit exports the filesystem as a tarball that mke2fs (e2fsprogs 1.47.1 or
newer) packs into ext4 without root or loop mounts; the kernel is fetched and
checked on the build host. The dev target tags the same build as a container
image for the Docker dev backend (docs/TASKS.md build decision 4), whose init
runs the same services and serves the agent over TCP behind a token.
"""

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import certifi

from rlworld.images.config import (
    Artifact,
    ArtifactLock,
    BuildCommand,
    BuildError,
    BuildPlan,
    LinuxBuildConfig,
    locale_parts,
)
from rlworld.guest.server import DEFAULT_VNC_SOCKET
from rlworld.images.recipes import DOWNLOAD_PLACEHOLDER, LinuxApp, linux_app
from rlworld.network.plan import ENV_SUBNET, GATEWAY_ADDRESS, GUEST_ADDRESS
from rlworld.specs.digest import spec_digest
from rlworld.specs.models import DisplaySpec, ImageSpec

BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off"
BASE_PACKAGES = ("systemd", "systemd-sysv", "dbus", "iproute2", "xvfb", "x11-xserver-utils", "openbox",
                 "at-spi2-core", "python3", "python3-venv", "python3-gi", "python3-pyatspi", "xdotool", "x11vnc",
                 "locales", "ca-certificates", "curl", "unzip", "xz-utils")
CA_PACKAGES = ("libnss3-tools",)
# Snapshot mirrors redirect http to https before the base image has a CA bundle, so the first apt step trusts
# the builder's pinned certifi bundle until ca-certificates is installed.
BOOTSTRAP_CA = "bootstrap_ca.pem"
# The overlay lands before apt, so dpkg keeps its files instead of stopping at a conffile prompt.
KEEP_OVERLAY_CONFFILES = 'Dpkg::Options { "--force-confdef"; "--force-confold"; };\n'
DOWNLOAD_DIRECTORY = "/tmp/rlworld/downloads"
INPUT_DIRECTORY = "/tmp/rlworld/inputs"
CA_DIRECTORY = "/usr/local/share/ca-certificates/rlworld"
RUNTIME_DIRECTORY = "/run/rlworld-desktop"
ACCESSIBILITY_BUS_CONFIG = "/usr/share/defaults/at-spi2/accessibility.conf"
GUEST_AGENT = "/opt/rlworld/bin/rlworld-guest-agent"
# The guest agent's Python, with openpyxl, that scripted oracles run as the desktop user.
GUEST_PYTHON = "/opt/rlworld/bin/python"
GUEST_REQUIREMENTS = "guest-requirements.txt"
CHROMIUM_POLICY_DIRECTORIES = {"chrome": "etc/opt/chrome_for_testing/policies/managed",
                               "edge": "etc/opt/edge/policies/managed"}
ENABLED_UNITS = ("rlworld-network.service", "rlworld-xvfb.service", "rlworld-desktop.service",
                 "rlworld-guest-agent.service", "rlworld-vnc.service")
SESSION_BUS_COMMAND = (f"/usr/bin/dbus-daemon --session --address=unix:path={RUNTIME_DIRECTORY}/bus"
                       " --nofork --nopidfile")
VNC_COMMAND = f"/usr/bin/x11vnc -display :0 -unixsockonly {DEFAULT_VNC_SOCKET} -forever -shared -nopw -noxdamage -quiet"
WAIT_FOR_DISPLAY = "while [ ! -S /tmp/.X11-unix/X0 ]; do sleep 0.1; done"
DEV_TOKEN_FILE = "/run/rlworld/agent.token"
DEV_AGENT_FLAGS = ("--transport", "tcp", "--token-file", DEV_TOKEN_FILE, "--container")
DEV_DOCKERFILE_SUFFIX = ("COPY dev/ /\n"
                         "RUN chmod 0755 /usr/local/bin/rlworld-dev-init\n"
                         'ENTRYPOINT ["/usr/local/bin/rlworld-dev-init"]\n')


@dataclass(frozen=True)
class LinuxHardening:
    """What one hardening name changes: build commands, overlay files and app settings."""

    commands: tuple[str, ...] = ()
    files: Mapping[str, str] = field(default_factory=dict)
    chromium_flags: tuple[str, ...] = ()
    chromium_policies: Mapping[str, Any] = field(default_factory=dict)
    firefox_policies: Mapping[str, Any] = field(default_factory=dict)
    gtk_settings: Mapping[str, str] = field(default_factory=dict)
    libreoffice_off: tuple[str, ...] = ()


LINUX_HARDENING = {
    "no_auto_update": LinuxHardening(
        commands=("ln -sf /dev/null /etc/systemd/system/apt-daily.timer",
                  "ln -sf /dev/null /etc/systemd/system/apt-daily-upgrade.timer"),
        files={"etc/apt/apt.conf.d/10rlworld-no-updates":
               'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Unattended-Upgrade "0";\n'},
        chromium_flags=("--disable-component-update",),
        chromium_policies={"ComponentUpdatesEnabled": False},
        firefox_policies={"DisableAppUpdate": True, "ExtensionUpdate": False}),
    "no_telemetry": LinuxHardening(
        chromium_flags=("--disable-background-networking",),
        chromium_policies={"MetricsReportingEnabled": False},
        firefox_policies={"DisableTelemetry": True, "DisableFirefoxStudies": True}),
    "no_first_run": LinuxHardening(
        chromium_flags=("--no-first-run", "--no-default-browser-check"),
        chromium_policies={"BrowserSignin": 0, "SyncDisabled": True, "HideFirstRunExperience": True},
        firefox_policies={"OverrideFirstRunPage": "", "OverridePostUpdatePage": "", "DontCheckDefaultBrowser": True,
                          "UserMessaging": {"SkipOnboarding": True, "ExtensionRecommendations": False,
                                            "FeatureRecommendations": False}},
        libreoffice_off=("Misc/ShowTipOfTheDay",)),
    "no_animations": LinuxHardening(
        chromium_flags=("--force-prefers-reduced-motion",),
        firefox_policies={"Preferences": {"ui.prefersReducedMotion": {"Value": 1, "Status": "locked"},
                                          "general.smoothScroll": {"Value": False, "Status": "locked"}}},
        gtk_settings={"gtk-enable-animations": "false"},
        libreoffice_off=("Accessibility/IsAllowAnimatedGraphics", "Accessibility/IsAllowAnimatedText")),
}


def linux_hardening(names: Sequence[str]) -> list[LinuxHardening]:
    unknown = [name for name in names if name not in LINUX_HARDENING]
    if unknown:
        known = ", ".join(LINUX_HARDENING)
        raise BuildError([f"hardening {name} does not apply to linux images; known: {known}" for name in unknown])
    return [LINUX_HARDENING[name] for name in names]


def base_reference(image: ImageSpec, config: LinuxBuildConfig) -> str:
    name, digest = image.base.split("@", 1)
    if name not in config.base_images:
        raise BuildError([f"base image {name} has no registry repository in the build config"])
    return f"{config.base_images[name]}@{digest}"


def systemd_argument(argument: str) -> str:
    escaped = argument.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def run_step(commands: Sequence[str]) -> str:
    return "RUN " + " \\\n && ".join(commands)


def download_path(name: str, artifact: Artifact) -> str:
    return f"{DOWNLOAD_DIRECTORY}/{name}/{artifact.file_name}"


def download_commands(name: str, artifact: Artifact) -> list[str]:
    path = download_path(name, artifact)
    return [f"mkdir -p {DOWNLOAD_DIRECTORY}/{name}", f"curl -fsSL --retry 3 -o {path} {shlex.quote(artifact.url)}",
            f"echo '{artifact.sha256}  {path}' | sha256sum --check --strict -"]


def desktop_environment(locale: str) -> str:
    """One environment for the session units and the agent user's login shells, so launched apps join the session."""
    variables = {"DISPLAY": ":0", "XDG_RUNTIME_DIR": RUNTIME_DIRECTORY,
                 "DBUS_SESSION_BUS_ADDRESS": f"unix:path={RUNTIME_DIRECTORY}/bus", "LANG": f"{locale}.UTF-8",
                 "ACCESSIBILITY_ENABLED": "1", "GNOME_ACCESSIBILITY": "1", "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
                 "SAL_USE_VCLPLUGIN": "gtk3"}
    return "".join(f"{name}={value}\n" for name, value in variables.items())


def gtk_settings(display: DisplaySpec, hardening: Sequence[LinuxHardening]) -> str:
    settings = {"gtk-theme-name": "Adwaita",
                "gtk-application-prefer-dark-theme": "true" if display.theme == "dark" else "false",
                "gtk-xft-dpi": str(display.dpi * 1024)}
    for step in hardening:
        settings.update(step.gtk_settings)
    return "[Settings]\n" + "".join(f"{name}={value}\n" for name, value in settings.items())


def libreoffice_registry(switched_off: Sequence[str]) -> str:
    """Common settings switched off for every user, as a registry layer LibreOffice loads after its own."""
    properties_by_node: dict[str, list[str]] = {}
    for path in switched_off:
        node, name = path.split("/")
        properties_by_node.setdefault(node, []).append(name)
    nodes = "".join(
        f'  <node oor:name="{node}">'
        + "".join(f'<prop oor:name="{name}" oor:op="fuse"><value>false</value></prop>' for name in names)
        + "</node>\n" for node, names in properties_by_node.items())
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<oor:data xmlns:xs="http://www.w3.org/2001/XMLSchema"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:oor="http://openoffice.org/2001/registry">\n'
            ' <dependency file="main"/>\n'
            ' <oor:component-data oor:package="org.openoffice.Office" oor:name="Common">\n'
            f"{nodes}"
            " </oor:component-data>\n"
            "</oor:data>\n")


def merged_policies(hardening: Sequence[LinuxHardening], attribute: str) -> dict[str, Any]:
    policies: dict[str, Any] = {}
    for step in hardening:
        policies.update(getattr(step, attribute))
    return policies


def policy_json(policies: Mapping[str, Any]) -> str:
    return json.dumps(policies, indent=2, sort_keys=True) + "\n"


def network_script() -> str:
    prefix_length = ENV_SUBNET.split("/")[1]
    return ("#!/bin/sh\nset -eu\n"
            "rm -f /etc/hosts /etc/resolv.conf\n"
            "cp /etc/rlworld/hosts /etc/hosts\n"
            "cp /etc/rlworld/resolv.conf /etc/resolv.conf\n"
            "ip link set lo up\n"
            "ip link set eth0 up\n"
            f"ip address add {GUEST_ADDRESS}/{prefix_length} dev eth0\n"
            f"ip route add default via {GATEWAY_ADDRESS}\n")


def desktop_script() -> str:
    """Waits for the display and session bus, starts the accessibility bus so it publishes itself on X, then openbox."""
    return ("#!/bin/sh\nset -eu\n"
            f"while [ ! -S /tmp/.X11-unix/X0 ] || [ ! -S {RUNTIME_DIRECTORY}/bus ]; do sleep 0.1; done\n"
            "xrdb -nocpp -merge /etc/rlworld/Xresources\n"
            "dbus-send --session --print-reply --dest=org.a11y.Bus /org/a11y/bus org.a11y.Bus.GetAddress > /dev/null\n"
            "exec openbox --startup /usr/local/bin/rlworld-autostart\n")


def autostart_script(probe_launch: str | None) -> str:
    """Opens the ready probe's window so the snapshot builder can wait for it."""
    return "#!/bin/sh\n" + (f"{probe_launch} &\n" if probe_launch else "exit 0\n")


def xvfb_command(display: DisplaySpec) -> str:
    return (f"/usr/bin/Xvfb :0 -screen 0 {display.width}x{display.height}x24 -dpi {display.dpi}"
            " -nolisten tcp -ac -s 0")


def guest_agent_argv(image: ImageSpec, config: LinuxBuildConfig, launchers: Mapping[str, str]) -> list[str]:
    return [GUEST_AGENT, "--user", config.user, "--screen", f"{image.display.width}x{image.display.height}",
            *(f"--launcher={window}={command}" for window, command in launchers.items())]


def dev_init_script(image: ImageSpec, config: LinuxBuildConfig, launchers: Mapping[str, str]) -> str:
    """The dev container's init. The token goes into a root only file and leaves the environment before anything
    starts, so no process the policy can inspect holds it. Then it starts what the units start in a VM except the
    network, which is Docker's; the agent serves TCP."""
    agent = " ".join(shlex.quote(argument)
                     for argument in [*guest_agent_argv(image, config, launchers), *DEV_AGENT_FLAGS])
    return ("#!/bin/sh\nset -eu\n"
            "install -d -m 0700 /run/rlworld\n"
            f'(umask 077 && printf %s "${{RLWORLD_AGENT_TOKEN:?}}" > {DEV_TOKEN_FILE})\n'
            "unset RLWORLD_AGENT_TOKEN\n"
            "set -a\n. /etc/rlworld/desktop.env\nset +a\n"
            f"{xvfb_command(image.display)} &\n"
            f"{WAIT_FOR_DISPLAY}\n"
            f"{VNC_COMMAND} &\n"
            f"install -d -m 0700 -o {config.user} -g {config.user} {RUNTIME_DIRECTORY}\n"
            f"runuser -u {config.user} -- {SESSION_BUS_COMMAND} &\n"
            f"runuser -u {config.user} -- sh -c 'cd \"$HOME\" && exec /usr/local/bin/rlworld-desktop' &\n"
            f"exec {agent}\n")


def unit_files(config: LinuxBuildConfig, display: DisplaySpec, agent_exec_start: str) -> dict[str, str]:
    return {
        "etc/systemd/system/rlworld-network.service": (
            "[Unit]\nDescription=rlworld guest network\nDefaultDependencies=no\nBefore=network.target\n\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/local/bin/rlworld-network\n\n"
            "[Install]\nWantedBy=multi-user.target\n"),
        "etc/systemd/system/rlworld-xvfb.service": (
            "[Unit]\nDescription=rlworld display\n\n"
            f"[Service]\nExecStart={xvfb_command(display)}\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n"),
        "etc/systemd/system/rlworld-session-bus.service": (
            "[Unit]\nDescription=rlworld desktop session bus\n\n"
            f"[Service]\nUser={config.user}\nEnvironmentFile=/etc/rlworld/desktop.env\n"
            "RuntimeDirectory=rlworld-desktop\nRuntimeDirectoryMode=0700\n"
            f"ExecStart={SESSION_BUS_COMMAND}\nRestart=always\n"),
        "etc/systemd/system/rlworld-desktop.service": (
            "[Unit]\nDescription=rlworld desktop session\n"
            "Requires=rlworld-xvfb.service rlworld-session-bus.service\n"
            "After=rlworld-xvfb.service rlworld-session-bus.service\n\n"
            f"[Service]\nUser={config.user}\nWorkingDirectory=~\nEnvironmentFile=/etc/rlworld/desktop.env\n"
            "ExecStart=/usr/local/bin/rlworld-desktop\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n"),
        "etc/systemd/system/rlworld-vnc.service": (
            "[Unit]\nDescription=rlworld screen for operator takeover\nRequires=rlworld-xvfb.service\n"
            "After=rlworld-xvfb.service\n\n"
            "[Service]\nRuntimeDirectory=rlworld\nRuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n"
            f"ExecStartPre=/bin/sh -c '{WAIT_FOR_DISPLAY}'\nExecStart={VNC_COMMAND}\nRestart=always\n\n"
            "[Install]\nWantedBy=multi-user.target\n"),
        "etc/systemd/system/rlworld-guest-agent.service": (
            "[Unit]\nDescription=rlworld guest agent on vsock\nAfter=rlworld-xvfb.service\n\n"
            f"[Service]\nEnvironment=DISPLAY=:0\nExecStart={agent_exec_start}\nRestart=always\n\n"
            "[Install]\nWantedBy=multi-user.target\n"),
    }


def overlay_files(image: ImageSpec, config: LinuxBuildConfig, apps: Mapping[str, LinuxApp],
                  hardening: Sequence[LinuxHardening], trusted_cas: Mapping[str, bytes]) -> dict[str, bytes]:
    """Files copied over the base image's root before anything is installed."""
    launchers = {recipe.window: recipe.launch for recipe in apps.values()}
    agent_argv = guest_agent_argv(image, config, launchers)
    probe_window = image.ready_probe.window
    files = {
        "etc/apt/sources.list": "".join(f"{line}\n" for line in config.apt_sources),
        "etc/apt/apt.conf.d/10rlworld-keep-overlay": KEEP_OVERLAY_CONFFILES,
        "etc/rlworld/desktop.env": desktop_environment(image.display.locale),
        "etc/rlworld/Xresources": f"Xft.dpi: {image.display.dpi}\n",
        "etc/rlworld/hosts": "127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n",
        "etc/rlworld/resolv.conf": f"nameserver {GATEWAY_ADDRESS}\n",
        "etc/profile.d/rlworld-desktop.sh": "set -a\n. /etc/rlworld/desktop.env\nset +a\n",
        "etc/gtk-3.0/settings.ini": gtk_settings(image.display, hardening),
        "etc/gtk-4.0/settings.ini": gtk_settings(image.display, hardening),
        "usr/local/bin/rlworld-network": network_script(),
        "usr/local/bin/rlworld-desktop": desktop_script(),
        "usr/local/bin/rlworld-autostart": autostart_script(launchers[probe_window] if probe_window else None),
        **unit_files(config, image.display, " ".join(systemd_argument(argument) for argument in agent_argv)),
    }
    for step in hardening:
        files.update(step.files)
    chromium_policies = merged_policies(hardening, "chromium_policies")
    for app, policy_directory in CHROMIUM_POLICY_DIRECTORIES.items():
        if app in apps:
            files[f"{policy_directory}/rlworld.json"] = policy_json(chromium_policies)
    if "edge" in apps:
        files["etc/default/microsoft-edge"] = 'repo_add_once="false"\nrepo_reenable_on_distupgrade="false"\n'
    if "firefox" in apps:
        firefox_policies = merged_policies(hardening, "firefox_policies")
        if trusted_cas:
            installs = [f"{CA_DIRECTORY}/{name}.crt" for name in sorted(trusted_cas)]
            firefox_policies["Certificates"] = {"Install": installs}
        files["etc/firefox/policies/policies.json"] = policy_json({"policies": firefox_policies})
    switched_off = [path for step in hardening for path in step.libreoffice_off]
    if "libreoffice" in apps and switched_off:
        files["usr/lib/libreoffice/share/registry/rlworld.xcd"] = libreoffice_registry(switched_off)
    encoded = {path: content.encode("utf-8") for path, content in files.items()}
    encoded.update({f"{CA_DIRECTORY.lstrip('/')}/{name}.crt": pem for name, pem in trusted_cas.items()})
    return encoded


def trust_commands(user: str, ca_names: Sequence[str], has_chromium: bool) -> list[str]:
    """The system store for most programs; Chromium browsers on Linux read the user's NSS database instead."""
    commands = ["update-ca-certificates"]
    if has_chromium:
        nssdb = f"/home/{user}/.pki/nssdb"
        commands += [f"runuser -u {user} -- mkdir -p {nssdb}",
                     f"runuser -u {user} -- certutil -d sql:{nssdb} -N --empty-password"]
        commands += [f"runuser -u {user} -- certutil -d sql:{nssdb} -A -t C,, -n {name}"
                     f" -i {CA_DIRECTORY}/{name}.crt" for name in ca_names]
    return commands


def render_dockerfile(image: ImageSpec, config: LinuxBuildConfig, apps: Mapping[str, LinuxApp],
                      downloads: Mapping[str, Artifact], hardening: Sequence[LinuxHardening],
                      ca_names: Sequence[str]) -> str:
    locale = image.display.locale
    has_chromium = any(app in apps for app in CHROMIUM_POLICY_DIRECTORIES)
    bootstrap_ca_option = f"-o Acquire::https::CaInfo={INPUT_DIRECTORY}/{BOOTSTRAP_CA}"
    packages = list(dict.fromkeys([*BASE_PACKAGES, *config.fonts, *(CA_PACKAGES if ca_names and has_chromium else ()),
                                   *(package for recipe in apps.values() for package in recipe.packages)]))
    steps = [
        "# syntax=docker/dockerfile:1",
        f"FROM {base_reference(image, config)}",
        "ENV DEBIAN_FRONTEND=noninteractive",
        'SHELL ["/bin/bash", "-euo", "pipefail", "-c"]',
        "COPY rootfs/ /",
        f"COPY inputs/ {INPUT_DIRECTORY}/",
        run_step(["rm -f /etc/apt/sources.list.d/*",
                  f"apt-get -o Acquire::Check-Valid-Until=false {bootstrap_ca_option} update",
                  f"apt-get {bootstrap_ca_option} install -y --no-install-recommends {' '.join(packages)}"]),
        run_step([f"localedef -i {locale} -f UTF-8 {locale}.UTF-8", f"update-locale LANG={locale}.UTF-8"]),
        # Ubuntu base images since 23.04 ship an ubuntu user holding UID 1000.
        run_step(["if getent passwd ubuntu > /dev/null; then userdel --remove ubuntu; fi",
                  f"useradd --create-home --uid 1000 --shell /bin/bash {config.user}", f"passwd --lock {config.user}"]),
    ]
    steps += [run_step(download_commands(name, artifact)) for name, artifact in downloads.items()]
    for recipe in apps.values():
        if recipe.install:
            archive = download_path(recipe.artifact, downloads[recipe.artifact]) if recipe.artifact else ""
            steps.append(run_step([line.replace(DOWNLOAD_PLACEHOLDER, archive) for line in recipe.install]))
    steps += [
        run_step(["python3 -m venv --system-site-packages /opt/rlworld",
                  "/opt/rlworld/bin/pip install --no-cache-dir --no-deps --require-hashes"
                  f" -r {INPUT_DIRECTORY}/{GUEST_REQUIREMENTS}",
                  "/opt/rlworld/bin/pip install --no-cache-dir --no-deps"
                  f" {INPUT_DIRECTORY}/{config.guest_agent_wheel.name}"]),
        run_step([f"""sed -i 's|<policy context="default">|<policy context="default">\\n    <allow user="root"/>|'"""
                  f" {ACCESSIBILITY_BUS_CONFIG}",
                  f"""grep -q '<allow user="root"/>' {ACCESSIBILITY_BUS_CONFIG}"""]),
    ]
    if ca_names:
        steps.append(run_step(trust_commands(config.user, ca_names, has_chromium)))
    hardening_commands = [command for step in hardening for command in step.commands]
    if hardening_commands:
        steps.append(run_step(hardening_commands))
    steps += [
        run_step(["chmod 0755 /usr/local/bin/rlworld-*", f"systemctl enable {' '.join(ENABLED_UNITS)}",
                  "ln -sf /dev/null /etc/systemd/system/systemd-timesyncd.service"]),
        run_step(["apt-get clean", "rm -rf /var/lib/apt/lists/* /tmp/rlworld"]),
    ]
    return "\n".join(steps) + "\n"


@dataclass(frozen=True)
class LinuxContext:
    """The docker build context both targets share: Dockerfile, root overlay and the guest agent's inputs."""

    files: dict[str, bytes]
    copies: dict[str, Path]
    downloads: dict[str, Artifact]
    launchers: dict[str, str]

    def manifest(self, image: ImageSpec, config: LinuxBuildConfig, artifacts: Mapping[str, Artifact]) -> dict[str, Any]:
        return {"base_image": base_reference(image, config), "apt_sources": list(config.apt_sources),
                "user": config.user, "launchers": self.launchers,
                "artifacts": {name: artifact.sha256 for name, artifact in artifacts.items()}}


def linux_context(image: ImageSpec, config: LinuxBuildConfig, lock: ArtifactLock,
                  trusted_cas: Mapping[str, bytes]) -> LinuxContext:
    locale_parts(image.display.locale)
    hardening = linux_hardening(image.hardening)
    chromium_flags = tuple(flag for step in hardening for flag in step.chromium_flags)
    apps = {app: linux_app(app, label, config.user, chromium_flags) for app, label in image.apps.items()}
    launchers = {recipe.window: recipe.launch for recipe in apps.values()}
    probe_window = image.ready_probe.window
    if probe_window is not None and probe_window not in launchers:
        raise BuildError([f"ready probe window {probe_window} is not opened by any app in the image"])
    downloads = {recipe.artifact: lock.require(recipe.artifact) for recipe in apps.values() if recipe.artifact}
    files = {"context/Dockerfile": render_dockerfile(image, config, apps, downloads, hardening,
                                                     sorted(trusted_cas)).encode()}
    files.update({f"context/rootfs/{path}": content
                  for path, content in overlay_files(image, config, apps, hardening, trusted_cas).items()})
    copies = {f"context/inputs/{config.guest_agent_wheel.name}": config.guest_agent_wheel,
              f"context/inputs/{GUEST_REQUIREMENTS}": config.guest_agent_requirements,
              f"context/inputs/{BOOTSTRAP_CA}": Path(certifi.where())}
    return LinuxContext(files=files, copies=copies, downloads=downloads, launchers=launchers)


def linux_build_plan(image: ImageSpec, config: LinuxBuildConfig, lock: ArtifactLock,
                     trusted_cas: Mapping[str, bytes]) -> BuildPlan:
    context = linux_context(image, config, lock, trusted_cas)
    kernel = lock.require(config.kernel_artifact)
    return BuildPlan(
        files=context.files,
        copies=context.copies,
        downloads={"vmlinux": kernel},
        commands=(
            BuildCommand(("docker", "build", "--platform", config.platform, "--output", "type=tar,dest=rootfs.tar",
                          "--file", "context/Dockerfile", "context")),
            BuildCommand(("mke2fs", "-F", "-q", "-t", "ext4", "-L", "rootfs", "-d", "rootfs.tar", "rootfs.ext4",
                          f"{config.rootfs_size_mib}M")),
            BuildCommand(("rm", "-f", "rootfs.tar")),
        ),
        outputs={"rootfs": "rootfs.ext4", "kernel": "vmlinux"},
        manifest={**context.manifest(image, config, {**context.downloads, config.kernel_artifact: kernel}),
                  "boot_args": BOOT_ARGS},
    )


def dev_image_tag(image: ImageSpec) -> str:
    return f"rlworld-dev/{image.id}:{spec_digest(image).removeprefix('sha256:')[:12]}"


def linux_dev_build_plan(image: ImageSpec, config: LinuxBuildConfig, lock: ArtifactLock,
                         trusted_cas: Mapping[str, bytes]) -> BuildPlan:
    """The same build tagged as a container image; its digest and tag are what dev leases name as the snapshot."""
    context = linux_context(image, config, lock, trusted_cas)
    tag = dev_image_tag(image)
    files = {**context.files,
             "context/Dockerfile": context.files["context/Dockerfile"] + DEV_DOCKERFILE_SUFFIX.encode(),
             "context/dev/usr/local/bin/rlworld-dev-init": dev_init_script(image, config, context.launchers).encode()}
    return BuildPlan(
        files=files,
        copies=context.copies,
        downloads={},
        commands=(BuildCommand(("docker", "build", "--platform", config.platform, "--tag", tag, "--iidfile",
                                "image.id", "--file", "context/Dockerfile", "context")),),
        outputs={"image_id": "image.id"},
        manifest={**context.manifest(image, config, context.downloads), "image_tag": tag, "agent_transport": "tcp"},
    )
