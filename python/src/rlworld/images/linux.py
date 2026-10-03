"""Linux images: a pinned Dockerfile becomes an ext4 rootfs plus a guest kernel (DESIGN.md sections 6.1 to 6.3).

The Dockerfile starts from the spec's base image by digest and installs from
dated snapshot mirrors only; downloads are checked against the artifact lock
and the guest agent's dependencies against their hashes. The image boots
systemd with the clone address behind NAT, Xvfb, openbox and a session bus
whose accessibility bus the root guest agent may read. The agent listens on
vsock and nothing else touches the clock, which the agent sets to world time.
BuildKit exports the filesystem as a tarball that mke2fs (e2fsprogs 1.47.1 or
newer) packs into ext4 without root or loop mounts; the kernel is fetched and
checked on the build host.
"""

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rlworld.images.config import (
    Artifact,
    ArtifactLock,
    BuildCommand,
    BuildError,
    BuildPlan,
    LinuxBuildConfig,
    locale_parts,
)
from rlworld.images.recipes import DOWNLOAD_PLACEHOLDER, LinuxApp, linux_app
from rlworld.network.plan import ENV_SUBNET, GATEWAY_ADDRESS, GUEST_ADDRESS
from rlworld.specs.models import DisplaySpec, ImageSpec

BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off"
BASE_PACKAGES = ("systemd", "systemd-sysv", "dbus", "iproute2", "xvfb", "x11-xserver-utils", "openbox",
                 "at-spi2-core", "python3", "python3-venv", "python3-gi", "python3-pyatspi", "xdotool", "locales",
                 "ca-certificates", "curl", "unzip", "xz-utils")
CA_PACKAGES = ("libnss3-tools",)
DOWNLOAD_DIRECTORY = "/tmp/rlworld/downloads"
INPUT_DIRECTORY = "/tmp/rlworld/inputs"
CA_DIRECTORY = "/usr/local/share/ca-certificates/rlworld"
RUNTIME_DIRECTORY = "/run/rlworld-desktop"
ACCESSIBILITY_BUS_CONFIG = "/usr/share/defaults/at-spi2/accessibility.conf"
GUEST_AGENT = "/opt/rlworld/bin/rlworld-guest-agent"
GUEST_REQUIREMENTS = "guest-requirements.txt"
CHROMIUM_POLICY_DIRECTORIES = {"chrome": "etc/opt/chrome_for_testing/policies/managed",
                               "edge": "etc/opt/edge/policies/managed"}
ENABLED_UNITS = ("rlworld-network.service", "rlworld-xvfb.service", "rlworld-desktop.service",
                 "rlworld-guest-agent.service")


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
    """Waits for the display, starts the accessibility bus so it publishes itself on X, then runs openbox."""
    return ("#!/bin/sh\nset -eu\n"
            "while [ ! -S /tmp/.X11-unix/X0 ]; do sleep 0.1; done\n"
            "xrdb -nocpp -merge /etc/rlworld/Xresources\n"
            "dbus-send --session --print-reply --dest=org.a11y.Bus /org/a11y/bus org.a11y.Bus.GetAddress > /dev/null\n"
            "exec openbox --startup /usr/local/bin/rlworld-autostart\n")


def autostart_script(probe_launch: str | None) -> str:
    """Opens the ready probe's window so the snapshot builder can wait for it."""
    return "#!/bin/sh\n" + (f"{probe_launch} &\n" if probe_launch else "exit 0\n")


def unit_files(config: LinuxBuildConfig, display: DisplaySpec, agent_exec_start: str) -> dict[str, str]:
    geometry = f"{display.width}x{display.height}"
    return {
        "etc/systemd/system/rlworld-network.service": (
            "[Unit]\nDescription=rlworld guest network\nDefaultDependencies=no\nBefore=network.target\n\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/local/bin/rlworld-network\n\n"
            "[Install]\nWantedBy=multi-user.target\n"),
        "etc/systemd/system/rlworld-xvfb.service": (
            "[Unit]\nDescription=rlworld display\n\n"
            f"[Service]\nExecStart=/usr/bin/Xvfb :0 -screen 0 {geometry}x24 -dpi {display.dpi}"
            " -nolisten tcp -ac -s 0\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n"),
        "etc/systemd/system/rlworld-session-bus.service": (
            "[Unit]\nDescription=rlworld desktop session bus\n\n"
            f"[Service]\nUser={config.user}\nEnvironmentFile=/etc/rlworld/desktop.env\n"
            "RuntimeDirectory=rlworld-desktop\nRuntimeDirectoryMode=0700\n"
            f"ExecStart=/usr/bin/dbus-daemon --session --address=unix:path={RUNTIME_DIRECTORY}/bus"
            " --nofork --nopidfile\nRestart=always\n"),
        "etc/systemd/system/rlworld-desktop.service": (
            "[Unit]\nDescription=rlworld desktop session\n"
            "Requires=rlworld-xvfb.service rlworld-session-bus.service\n"
            "After=rlworld-xvfb.service rlworld-session-bus.service\n\n"
            f"[Service]\nUser={config.user}\nWorkingDirectory=~\nEnvironmentFile=/etc/rlworld/desktop.env\n"
            "ExecStart=/usr/local/bin/rlworld-desktop\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n"),
        "etc/systemd/system/rlworld-guest-agent.service": (
            "[Unit]\nDescription=rlworld guest agent on vsock\nAfter=rlworld-xvfb.service\n\n"
            f"[Service]\nEnvironment=DISPLAY=:0\nExecStart={agent_exec_start}\nRestart=always\n\n"
            "[Install]\nWantedBy=multi-user.target\n"),
    }


def overlay_files(image: ImageSpec, config: LinuxBuildConfig, apps: Mapping[str, LinuxApp],
                  hardening: Sequence[LinuxHardening], trusted_cas: Mapping[str, bytes]) -> dict[str, bytes]:
    """Files copied over the base image's root before anything is installed."""
    launchers = {recipe.window: recipe.launch for recipe in apps.values()}
    agent_argv = [GUEST_AGENT, "--user", config.user, "--screen", f"{image.display.width}x{image.display.height}",
                  *(f"--launcher={window}={command}" for window, command in launchers.items())]
    probe_window = image.ready_probe.window
    files = {
        "etc/apt/sources.list": "".join(f"{line}\n" for line in config.apt_sources),
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
    packages = list(dict.fromkeys([*BASE_PACKAGES, *config.fonts, *(CA_PACKAGES if ca_names and has_chromium else ()),
                                   *(package for recipe in apps.values() for package in recipe.packages)]))
    steps = [
        "# syntax=docker/dockerfile:1",
        f"FROM {base_reference(image, config)}",
        "ENV DEBIAN_FRONTEND=noninteractive",
        'SHELL ["/bin/bash", "-euo", "pipefail", "-c"]',
        "COPY rootfs/ /",
        f"COPY inputs/ {INPUT_DIRECTORY}/",
        run_step(["rm -f /etc/apt/sources.list.d/*", "apt-get -o Acquire::Check-Valid-Until=false update",
                  f"apt-get install -y --no-install-recommends {' '.join(packages)}"]),
        run_step([f"localedef -i {locale} -f UTF-8 {locale}.UTF-8", f"update-locale LANG={locale}.UTF-8"]),
        run_step([f"useradd --create-home --uid 1000 --shell /bin/bash {config.user}", f"passwd --lock {config.user}"]),
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


def linux_build_plan(image: ImageSpec, config: LinuxBuildConfig, lock: ArtifactLock,
                     trusted_cas: Mapping[str, bytes]) -> BuildPlan:
    locale_parts(image.display.locale)
    hardening = linux_hardening(image.hardening)
    chromium_flags = tuple(flag for step in hardening for flag in step.chromium_flags)
    apps = {app: linux_app(app, label, config.user, chromium_flags) for app, label in image.apps.items()}
    launchers = {recipe.window: recipe.launch for recipe in apps.values()}
    probe_window = image.ready_probe.window
    if probe_window is not None and probe_window not in launchers:
        raise BuildError([f"ready probe window {probe_window} is not opened by any app in the image"])
    downloads = {recipe.artifact: lock.require(recipe.artifact) for recipe in apps.values() if recipe.artifact}
    kernel = lock.require(config.kernel_artifact)
    artifacts = {**downloads, config.kernel_artifact: kernel}
    ca_names = sorted(trusted_cas)
    files = {"context/Dockerfile": render_dockerfile(image, config, apps, downloads, hardening, ca_names).encode()}
    files.update({f"context/rootfs/{path}": content
                  for path, content in overlay_files(image, config, apps, hardening, trusted_cas).items()})
    return BuildPlan(
        files=files,
        copies={f"context/inputs/{config.guest_agent_wheel.name}": config.guest_agent_wheel,
                f"context/inputs/{GUEST_REQUIREMENTS}": config.guest_agent_requirements},
        downloads={"vmlinux": kernel},
        commands=(
            BuildCommand(("docker", "build", "--platform", config.platform, "--output", "type=tar,dest=rootfs.tar",
                          "--file", "context/Dockerfile", "context")),
            BuildCommand(("mke2fs", "-F", "-q", "-t", "ext4", "-L", "rootfs", "-d", "rootfs.tar", "rootfs.ext4",
                          f"{config.rootfs_size_mib}M")),
            BuildCommand(("rm", "-f", "rootfs.tar")),
        ),
        outputs={"rootfs": "rootfs.ext4", "kernel": "vmlinux"},
        manifest={"base_image": base_reference(image, config), "apt_sources": list(config.apt_sources),
                  "boot_args": BOOT_ARGS, "user": config.user, "launchers": launchers,
                  "artifacts": {name: artifact.sha256 for name, artifact in artifacts.items()}},
    )
