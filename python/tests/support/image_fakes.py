"""Shared inputs for image pipeline tests: specs, build configs, an artifact lock, a CA and fake build tools."""

from datetime import datetime, timezone
from pathlib import Path

from rlworld.images.config import ArtifactLock, LinuxBuildConfig, WindowsBuildConfig
from rlworld.network import create_certificate_authority
from rlworld.specs.models import ImageSpec

DIGEST = "a" * 64
WORLD_TIME = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)
LOCK = ArtifactLock.model_validate({"artifacts": {
    "chrome_130.0.6723.58": {"url": "https://storage.example/cft/chrome-linux64.zip", "sha256": "1" * 64},
    "firefox_128.5.0esr": {"url": "https://ftp.example/firefox-128.5.0esr.tar.xz?product=esr", "sha256": "2" * 64},
    "thunderbird_153.4.0esr": {"url": "https://ftp.example/thunderbird-153.4.0esr.tar.xz", "sha256": "a" * 64},
    "edge_131.0.2903.70": {"url": "https://msedge.example/microsoft-edge-stable_131_amd64.deb", "sha256": "3" * 64},
    "guest_kernel": {"url": "https://kernels.example/vmlinux-6.1", "sha256": "4" * 64},
    "office_deployment_tool": {"url": "https://download.example/officedeploymenttool.exe", "sha256": "5" * 64},
    "edge_131.0.2903.71": {"url": "https://msedge.example/MicrosoftEdgeEnterpriseX64.msi", "sha256": "6" * 64},
    "rlworld_windows_helper": {"url": "https://artifacts.example/rlworld-windows-helper.zip", "sha256": "7" * 64},
    "windows_server_2025": {"url": "https://iso.example/server2025.iso", "sha256": "8" * 64},
    "virtio_win": {"url": "https://fedora.example/virtio-win-0.1.266.iso", "sha256": "9" * 64},
}})


def linux_image(**changes):
    payload = {
        "id": "noble_browsers_v1", "os": "linux", "base": f"ubuntu_noble@sha256:{DIGEST}",
        "pool": {"vmm": "firecracker", "cpu_model": "fleet_baseline"},
        "apps": {"chrome": "130.0.6723.58", "firefox": "128.5.0esr", "libreoffice": "4:24.2.7-0ubuntu0.24.04.1"},
        "display": {"width": 1280, "height": 800, "dpi": 96, "theme": "light", "locale": "en_US"},
        "hardening": ["no_auto_update", "no_first_run", "no_animations", "no_telemetry"],
        "ready_probe": {"window": "Chrome"},
    }
    payload.update(changes)
    return ImageSpec.model_validate(payload)


def windows_image(**changes):
    payload = {
        "id": "win2025_office_v4", "os": "windows", "base": f"windows_server_2025_desktop@sha256:{DIGEST}",
        "pool": {"vmm": "qemu_templated", "machine": "pc-q35-9.0", "cpu_model": "Cascadelake-Server-v5"},
        "apps": {"excel": "office_ltsc_2024", "edge": "131.0.2903.71"},
        "display": {"width": 1280, "height": 800, "dpi": 120, "theme": "dark", "locale": "en_US"},
        "hardening": ["no_auto_update", "no_defender_scan", "no_first_run", "no_protected_view", "no_animations"],
        "ready_probe": {"window": "Excel"},
    }
    payload.update(changes)
    return ImageSpec.model_validate(payload)


def linux_config(**changes):
    payload = {"base_images": {"ubuntu_noble": "docker.io/library/ubuntu"},
               "apt_sources": ("deb http://snapshot.ubuntu.com/ubuntu/20260301T000000Z noble main universe",),
               "guest_agent_wheel": Path("dist/rlworld-0.1.0-py3-none-any.whl"),
               "guest_agent_requirements": Path("dist/guest.txt")}
    payload.update(changes)
    return LinuxBuildConfig.model_validate(payload)


def windows_config(**changes):
    payload = {"product_key": "D764K-2NDRG-47T6Q-P8T8W-YP6DF", "kms_host": "kms.internal:1688",
               "office_builds": {"office_ltsc_2024": "16.0.17932.20222"}}
    payload.update(changes)
    return WindowsBuildConfig.model_validate(payload)


def world_ca_pem():
    return create_certificate_authority("World CA", WORLD_TIME).certificate_pem


class RecordingRunner:
    """Records each command and creates the files a real tool would have produced."""

    def __init__(self, creates=None):
        self.creates = creates or {}
        self.commands = []

    def run(self, argv, working_directory, environment):
        self.commands.append((tuple(argv), working_directory, dict(environment)))
        for relative_path, content in self.creates.get(argv[0], {}).items():
            target = Path(working_directory) / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


class RecordingFetcher:
    def __init__(self, content=b"kernel"):
        self.content = content
        self.fetched = []

    def fetch(self, artifact, destination):
        self.fetched.append((artifact.url, destination))
        destination.write_bytes(self.content)
