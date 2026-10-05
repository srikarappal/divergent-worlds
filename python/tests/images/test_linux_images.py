import json
from pathlib import Path

import certifi
import pytest
from support.image_fakes import DIGEST, LOCK, linux_config, linux_image, world_ca_pem

from rlworld.images import BuildError, dev_image_tag, linux_build_plan, linux_dev_build_plan
from rlworld.images.linux import (
    BASE_PACKAGES,
    BOOT_ARGS,
    DEV_TOKEN_FILE,
    ENABLED_UNITS,
    dev_init_script,
    service_unit,
    systemd_argument,
)
from rlworld.images.recipes import LinuxService
from rlworld.network.plan import GATEWAY_ADDRESS, GUEST_ADDRESS
from rlworld.specs.digest import spec_digest


def plan_for(image=None, config=None, trusted_cas=None):
    return linux_build_plan(image or linux_image(), config or linux_config(), LOCK, trusted_cas or {})


def text(plan, path):
    return plan.files[path].decode("utf-8")


def test_dockerfile_starts_from_the_pinned_base_and_installs_from_snapshots_only():
    plan = plan_for()
    dockerfile = text(plan, "context/Dockerfile")
    assert f"FROM docker.io/library/ubuntu@sha256:{DIGEST}\n" in dockerfile
    assert "RUN rm -f /etc/apt/sources.list.d/*" in dockerfile
    assert dockerfile.index("COPY aptroot/ /") < dockerfile.index("apt-get -o")
    assert dockerfile.index("COPY inputs/bootstrap_ca.pem /tmp/rlworld/inputs/") < dockerfile.index("apt-get -o")
    assert dockerfile.index("apt-get -o") < dockerfile.index("COPY rootfs/ /") < dockerfile.index("COPY inputs/ ")
    assert ("apt-get -o Acquire::Check-Valid-Until=false"
            " -o Acquire::https::CaInfo=/tmp/rlworld/inputs/bootstrap_ca.pem update") in dockerfile
    assert "context/inputs/bootstrap_ca.pem" in plan.copies
    assert text(plan, "context/aptroot/etc/apt/apt.conf.d/10rlworld-keep-overlay") == (
        'Dpkg::Options { "--force-confdef"; "--force-confold"; };\n')
    assert text(plan, "context/aptroot/etc/apt/sources.list") == (
        "deb http://snapshot.ubuntu.com/ubuntu/20260301T000000Z noble main universe\n")
    install_line = next(line for line in dockerfile.splitlines() if "bootstrap_ca.pem install -y" in line)
    packages = install_line.split("--no-install-recommends ")[1].split()
    assert packages[:len(BASE_PACKAGES)] == list(BASE_PACKAGES)
    assert {"fonts-dejavu-core", "libreoffice-calc=4:24.2.7-0ubuntu0.24.04.1",
            "libreoffice-gtk3=4:24.2.7-0ubuntu0.24.04.1", "libgtk-3-0t64"} <= set(packages)
    assert "libnss3-tools" not in packages
    assert dockerfile.rstrip().endswith("rm -rf /var/lib/apt/lists/* /tmp/rlworld")


def test_downloads_are_checked_against_the_lock_before_apps_install():
    dockerfile = text(plan_for(), "context/Dockerfile")
    chrome_path = "/tmp/rlworld/downloads/chrome_130.0.6723.58/chrome-linux64.zip"
    firefox_path = "/tmp/rlworld/downloads/firefox_128.5.0esr/firefox-128.5.0esr.tar.xz"
    assert f"curl -fsSL --retry 3 -o {chrome_path} https://storage.example/cft/chrome-linux64.zip" in dockerfile
    assert f"echo '{'1' * 64}  {chrome_path}' | sha256sum --check --strict -" in dockerfile
    assert f"-o {firefox_path} 'https://ftp.example/firefox-128.5.0esr.tar.xz?product=esr'" in dockerfile
    assert dockerfile.index("sha256sum --check") < dockerfile.index(f"unzip -q {chrome_path} -d /opt")
    assert f"tar -xf {firefox_path} -C /opt" in dockerfile
    assert dockerfile.index("userdel --remove ubuntu") < dockerfile.index("useradd --create-home --uid 1000")
    assert dockerfile.index("useradd --create-home --uid 1000") < dockerfile.index("unzip -q")


def test_guest_agent_installs_from_hashed_requirements_and_reads_accessibility_as_root():
    plan = plan_for()
    dockerfile = text(plan, "context/Dockerfile")
    assert "python3 -m venv --system-site-packages /opt/rlworld" in dockerfile
    assert "--require-hashes -r /tmp/rlworld/inputs/guest-requirements.txt" in dockerfile
    assert "pip install --no-cache-dir --no-deps /tmp/rlworld/inputs/rlworld-0.1.0-py3-none-any.whl" in dockerfile
    assert '<allow user="root"/>' in dockerfile and "grep -q" in dockerfile
    assert plan.copies == {"context/inputs/rlworld-0.1.0-py3-none-any.whl": linux_config().guest_agent_wheel,
                           "context/inputs/guest-requirements.txt": linux_config().guest_agent_requirements,
                           "context/inputs/bootstrap_ca.pem": Path(certifi.where())}
    assert f"systemctl enable {' '.join(ENABLED_UNITS)}" in dockerfile
    assert "ln -sf /dev/null /etc/systemd/system/systemd-timesyncd.service" in dockerfile


def test_units_run_the_display_session_and_agent_with_spec_geometry():
    plan = plan_for(linux_image(display={"width": 1920, "height": 1080, "dpi": 144, "theme": "dark",
                                         "locale": "de_DE"}))
    xvfb = text(plan, "context/rootfs/etc/systemd/system/rlworld-xvfb.service")
    assert "ExecStart=/usr/bin/Xvfb :0 -screen 0 1920x1080x24 -dpi 144 -nolisten tcp -ac -s 0" in xvfb
    agent = text(plan, "context/rootfs/etc/systemd/system/rlworld-guest-agent.service")
    assert 'ExecStart="/opt/rlworld/bin/rlworld-guest-agent" "--user" "agent" "--screen" "1920x1080"' in agent
    assert '"--launcher=Calc=libreoffice --calc --nologo --norestore"' in agent
    assert "Environment=DISPLAY=:0" in agent
    session_bus = text(plan, "context/rootfs/etc/systemd/system/rlworld-session-bus.service")
    assert "User=agent" in session_bus and "--address=unix:path=/run/rlworld-desktop/bus" in session_bus
    desktop = text(plan, "context/rootfs/etc/systemd/system/rlworld-desktop.service")
    assert "Requires=rlworld-xvfb.service rlworld-session-bus.service" in desktop
    environment = text(plan, "context/rootfs/etc/rlworld/desktop.env")
    assert "LANG=de_DE.UTF-8\n" in environment and "ACCESSIBILITY_ENABLED=1\n" in environment
    assert text(plan, "context/rootfs/etc/rlworld/Xresources") == "Xft.dpi: 144\n"
    gtk = text(plan, "context/rootfs/etc/gtk-3.0/settings.ini")
    assert "gtk-application-prefer-dark-theme=true\n" in gtk and "gtk-xft-dpi=147456\n" in gtk
    assert "gtk-enable-animations=false\n" in gtk
    assert "localedef -i de_DE -f UTF-8 de_DE.UTF-8" in text(plan, "context/Dockerfile")


def test_systemd_arguments_escape_specifiers_and_quotes():
    assert systemd_argument('say "100%" $HOME') == '"say \\"100%%\\" $$HOME"'


def test_network_and_name_resolution_match_the_clone_network_plan():
    plan = plan_for()
    network = text(plan, "context/rootfs/usr/local/bin/rlworld-network")
    assert f"ip address add {GUEST_ADDRESS}/24 dev eth0\n" in network
    assert f"ip route add default via {GATEWAY_ADDRESS}\n" in network
    assert text(plan, "context/rootfs/etc/rlworld/resolv.conf") == f"nameserver {GATEWAY_ADDRESS}\n"
    assert "127.0.0.1 localhost" in text(plan, "context/rootfs/etc/rlworld/hosts")


def test_ready_probe_window_opens_at_session_start():
    plan = plan_for()
    autostart = text(plan, "context/rootfs/usr/local/bin/rlworld-autostart")
    assert autostart.startswith("#!/bin/sh\n/opt/chrome/chrome ") and autostart.endswith(" &\n")
    no_probe = plan_for(linux_image(ready_probe={"screen_stable_ms": 500}))
    assert text(no_probe, "context/rootfs/usr/local/bin/rlworld-autostart") == "#!/bin/sh\nexit 0\n"
    with pytest.raises(BuildError, match="ready probe window Excel"):
        plan_for(linux_image(ready_probe={"window": "Excel"}))
    assert "openbox --startup /usr/local/bin/rlworld-autostart" in text(
        plan, "context/rootfs/usr/local/bin/rlworld-desktop")


def test_hardening_reaches_browsers_office_and_apt():
    plan = plan_for()
    dockerfile = text(plan, "context/Dockerfile")
    assert "ln -sf /dev/null /etc/systemd/system/apt-daily.timer" in dockerfile
    no_updates = text(plan, "context/aptroot/etc/apt/apt.conf.d/10rlworld-no-updates")
    assert 'APT::Periodic::Unattended-Upgrade "0";' in no_updates
    chromium = json.loads(text(plan, "context/rootfs/etc/opt/chrome_for_testing/policies/managed/rlworld.json"))
    assert chromium["ComponentUpdatesEnabled"] is False and chromium["MetricsReportingEnabled"] is False
    assert "context/rootfs/etc/opt/edge/policies/managed/rlworld.json" not in plan.files
    firefox = json.loads(text(plan, "context/rootfs/etc/firefox/policies/policies.json"))["policies"]
    assert firefox["DisableAppUpdate"] is True and firefox["DisableTelemetry"] is True
    assert firefox["Preferences"]["ui.prefersReducedMotion"] == {"Value": 1, "Status": "locked"}
    assert "Certificates" not in firefox
    registry = text(plan, "context/rootfs/etc/libreoffice/registry/rlworld.xcd")
    assert '<prop oor:name="ShowTipOfTheDay" oor:op="fuse"><value>false</value></prop>' in registry
    assert '<node oor:name="Accessibility">' in registry
    chrome_launch = plan.manifest["launchers"]["Chrome"]
    for flag in ("--disable-component-update", "--no-first-run", "--force-prefers-reduced-motion",
                 "--disable-background-networking"):
        assert flag in chrome_launch


def test_unhardened_images_keep_defaults():
    plan = plan_for(linux_image(hardening=[]))
    assert "context/rootfs/etc/libreoffice/registry/rlworld.xcd" not in plan.files
    assert "apt-daily" not in text(plan, "context/Dockerfile")
    assert plan.manifest["launchers"]["Chrome"].endswith("--start-maximized --user-data-dir=/home/agent/.config"
                                                         "/rlworld-chrome")


def test_unknown_and_windows_only_hardening_fail_the_build():
    with pytest.raises(BuildError) as raised:
        plan_for(linux_image(hardening=["no_defender_scan", "no_auto_update", "no_screen_savers"]))
    assert len(raised.value.problems) == 2
    assert raised.value.problems[0].startswith("hardening no_defender_scan does not apply to linux images")


def test_world_cas_reach_the_system_store_nss_and_firefox():
    pem = world_ca_pem()
    plan = plan_for(trusted_cas={"world_ca": pem})
    assert plan.files["context/rootfs/usr/local/share/ca-certificates/rlworld/world_ca.crt"] == pem
    dockerfile = text(plan, "context/Dockerfile")
    assert "libnss3-tools" in dockerfile and "update-ca-certificates" in dockerfile
    assert ("runuser -u agent -- certutil -d sql:/home/agent/.pki/nssdb -A -t C,, -n world_ca"
            " -i /usr/local/share/ca-certificates/rlworld/world_ca.crt") in dockerfile
    firefox = json.loads(text(plan, "context/rootfs/etc/firefox/policies/policies.json"))["policies"]
    assert firefox["Certificates"] == {"Install": ["/usr/local/share/ca-certificates/rlworld/world_ca.crt"]}
    calc_only = plan_for(linux_image(apps={"libreoffice": "4:24.2.7"}, ready_probe={"window": "Calc"}),
                         trusted_cas={"world_ca": pem})
    assert "update-ca-certificates" in text(calc_only, "context/Dockerfile")
    assert "certutil" not in text(calc_only, "context/Dockerfile")
    assert "libnss3-tools" not in text(calc_only, "context/Dockerfile")


def test_edge_installs_its_package_without_adding_a_moving_repository():
    plan = plan_for(linux_image(apps={"edge": "131.0.2903.70"}, ready_probe={"window": "Edge"}))
    assert text(plan, "context/rootfs/etc/default/microsoft-edge").startswith('repo_add_once="false"')
    assert ("apt-get install -y --no-install-recommends "
            "/tmp/rlworld/downloads/edge_131.0.2903.70/microsoft-edge-stable_131_amd64.deb") in text(
        plan, "context/Dockerfile")
    assert "context/rootfs/etc/opt/edge/policies/managed/rlworld.json" in plan.files


def test_base_images_need_a_registry_repository():
    with pytest.raises(BuildError, match="base image ubuntu_noble has no registry repository"):
        plan_for(config=linux_config(base_images={}))


def test_build_exports_a_tarball_into_ext4_and_fetches_the_kernel():
    plan = plan_for(config=linux_config(rootfs_size_mib=4096, platform="linux/arm64"))
    assert [command.argv for command in plan.commands] == [
        ("docker", "build", "--platform", "linux/arm64", "--output", "type=tar,dest=rootfs.tar", "--file",
         "context/Dockerfile", "context"),
        ("mke2fs", "-F", "-q", "-t", "ext4", "-L", "rootfs", "-d", "rootfs.tar", "rootfs.ext4", "4096M"),
        ("rm", "-f", "rootfs.tar"),
    ]
    assert plan.downloads == {"vmlinux": LOCK.require("guest_kernel")}
    assert plan.outputs == {"rootfs": "rootfs.ext4", "kernel": "vmlinux"}
    assert plan.manifest["boot_args"] == BOOT_ARGS
    assert plan.manifest["artifacts"] == {"chrome_130.0.6723.58": "1" * 64, "firefox_128.5.0esr": "2" * 64,
                                          "guest_kernel": "4" * 64}
    with pytest.raises(BuildError, match="artifact guest_kernel_7 is not in the lock"):
        plan_for(config=linux_config(kernel_artifact="guest_kernel_7"))


def test_desktop_waits_for_the_display_and_the_session_bus():
    desktop = text(plan_for(), "context/rootfs/usr/local/bin/rlworld-desktop")
    assert "while [ ! -S /tmp/.X11-unix/X0 ] || [ ! -S /run/rlworld-desktop/bus ]; do sleep 0.1; done" in desktop


def test_dev_target_tags_the_same_build_as_a_container_image():
    vm_plan = plan_for()
    dev_plan = linux_dev_build_plan(linux_image(), linux_config(), LOCK, {})
    dockerfile = text(dev_plan, "context/Dockerfile")
    assert dockerfile.startswith(text(vm_plan, "context/Dockerfile"))
    assert dockerfile.endswith('COPY dev/ /\nRUN chmod 0755 /usr/local/bin/rlworld-dev-init\n'
                               'ENTRYPOINT ["/usr/local/bin/rlworld-dev-init"]\n')
    tag = dev_image_tag(linux_image())
    assert tag == f"rlworld-dev/noble_browsers_v1:{spec_digest(linux_image())[7:19]}"
    assert [command.argv for command in dev_plan.commands] == [
        ("docker", "build", "--platform", "linux/amd64", "--tag", tag, "--iidfile", "image.id", "--file",
         "context/Dockerfile", "context")]
    assert dev_plan.downloads == {} and dev_plan.outputs == {"image_id": "image.id"}
    assert dev_plan.manifest["image_tag"] == tag and dev_plan.manifest["agent_transport"] == "tcp"
    assert "guest_kernel" not in dev_plan.manifest["artifacts"] and "boot_args" not in dev_plan.manifest
    assert dev_plan.copies == vm_plan.copies


def test_the_screen_is_served_for_takeover_on_a_root_only_socket():
    plan = plan_for()
    unit = text(plan, "context/rootfs/etc/systemd/system/rlworld-vnc.service")
    assert "ExecStart=/usr/bin/x11vnc -display :0 -unixsock /run/rlworld/vnc.sock -rfbport 0" in unit
    assert "RuntimeDirectoryMode=0700" in unit and "Requires=rlworld-xvfb.service" in unit
    assert "rlworld-vnc.service" in text(plan, "context/Dockerfile") and "x11vnc" in text(plan, "context/Dockerfile")
    init = text(linux_dev_build_plan(linux_image(), linux_config(), LOCK, {}),
                "context/dev/usr/local/bin/rlworld-dev-init")
    assert init.index("x11vnc") > init.index("/usr/bin/Xvfb") and init.index("x11vnc") > init.index("X11-unix/X0")


def test_dev_init_hides_the_token_before_starting_anything():
    init = text(linux_dev_build_plan(linux_image(), linux_config(), LOCK, {}),
                "context/dev/usr/local/bin/rlworld-dev-init")
    lines = init.splitlines()
    token_line = lines.index(f'(umask 077 && printf %s "${{RLWORLD_AGENT_TOKEN:?}}" > {DEV_TOKEN_FILE})')
    assert lines[token_line + 1] == "unset RLWORLD_AGENT_TOKEN"
    xvfb_line = next(index for index, line in enumerate(lines) if line.startswith("/usr/bin/Xvfb :0"))
    assert token_line < xvfb_line
    assert "runuser -u agent -- /usr/bin/dbus-daemon --session --address=unix:path=/run/rlworld-desktop/bus" in init
    agent = lines[-1]
    assert agent.startswith("exec /opt/rlworld/bin/rlworld-guest-agent --user agent --screen 1280x800 ")
    assert agent.endswith(f" --transport tcp --token-file {DEV_TOKEN_FILE} --container")
    assert "'--launcher=Calc=libreoffice --calc --nologo --norestore'" in agent


def test_dev_init_holds_the_agent_back_until_the_ready_window_shows():
    init = text(linux_dev_build_plan(linux_image(), linux_config(), LOCK, {}),
                "context/dev/usr/local/bin/rlworld-dev-init")
    lines = init.splitlines()
    desktop_line = next(index for index, line in enumerate(lines) if "rlworld-desktop" in line)
    wait_line = lines.index("until xdotool search --onlyvisible --name Chrome > /dev/null 2>&1; do")
    assert desktop_line < wait_line < len(lines) - 1 and lines[-1].startswith("exec ")
    assert lines[-2] == "sleep 0.5" and lines[wait_line - 1].startswith("deadline=$(( $(date +%s) + ")


def test_xfce_images_bring_a_full_desktop_with_preferred_apps_and_a_preset_panel():
    plan = plan_for(linux_image(desktop="xfce"))
    dockerfile = text(plan, "context/Dockerfile")
    install_line = next(line for line in dockerfile.splitlines() if "bootstrap_ca.pem install -y" in line)
    packages = install_line.split("--no-install-recommends ")[1].split()
    assert {"xfce4-session", "xfce4-panel", "thunar", "xfce4-terminal", "mousepad"} <= set(packages)
    assert "openbox" not in packages
    assert "cp /etc/xdg/xfce4/panel/default.xml /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml" in dockerfile
    assert text(plan, "context/rootfs/usr/local/bin/rlworld-desktop").endswith("exec xfce4-session\n")
    autostart = text(plan, "context/rootfs/etc/xdg/autostart/rlworld-autostart.desktop")
    assert "Exec=/usr/local/bin/rlworld-autostart" in autostart
    assert "WebBrowser=firefox" in text(plan, "context/rootfs/etc/xdg/xfce4/helpers.rc")
    xfwm4 = text(plan, "context/rootfs/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml")
    assert '<property name="use_compositing" type="bool" value="false"/>' in xfwm4
    assert "Exec=firefox %u" in text(plan, "context/rootfs/usr/share/applications/rlworld-firefox.desktop")
    openbox_plan = plan_for()
    assert "openbox" in text(openbox_plan, "context/Dockerfile")
    assert "context/rootfs/etc/xdg/xfce4/helpers.rc" not in openbox_plan.files


def test_app_services_run_as_the_desktop_user_from_units_and_the_dev_init():
    service = LinuxService("mail", "/opt/rlworld/bin/rlworld-mail-server")
    unit = service_unit(linux_config(), service)
    assert "User=agent" in unit and "WorkingDirectory=~" in unit
    assert "ExecStart=/opt/rlworld/bin/rlworld-mail-server" in unit
    init = dev_init_script(linux_image(), linux_config(), {}, [service])
    assert "runuser -u agent -- sh -c 'cd \"$HOME\" && exec /opt/rlworld/bin/rlworld-mail-server' &" in init
    assert init.index("rlworld-mail-server") < init.index("exec /usr/local/bin/rlworld-desktop")
