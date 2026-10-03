"""Image build inputs and the plan a build follows (DESIGN.md sections 6.2 and 7).

Every input is pinned: base images by digest, apt mirrors by snapshot date,
Python packages by hash, Office by build number and downloaded installers by
URL and sha256 in an artifact lock, so an image can be rebuilt from its spec
and a drifting mirror fails the build instead of a run. Paths in the build
config are relative to the config file. The world CAs listed here are trusted
by every image, so HTTPS to cloned hostnames works unchanged (section 6.6).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import yaml
from cryptography import x509
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CaName = Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
OfficeBuild = Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]+){3}$")]
SNAPSHOT_MIRROR_HOSTS = ("snapshot.debian.org", "snapshot.ubuntu.com")
LINUX_PATH_FIELDS = ("guest_agent_wheel", "guest_agent_requirements")
LOCALE_PATTERN = re.compile(r"^([a-z]{2,3})_([A-Z]{2})$")


class BuildError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Artifact(FrozenModel):
    url: str = Field(pattern=r"^https://")
    sha256: Sha256

    @property
    def file_name(self) -> str:
        return self.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]


class ArtifactLock(FrozenModel):
    artifacts: dict[str, Artifact] = Field(default_factory=dict)

    def require(self, name: str) -> Artifact:
        if name not in self.artifacts:
            raise BuildError([f"artifact {name} is not in the lock; pin its URL and sha256 first"])
        return self.artifacts[name]


class LinuxBuildConfig(FrozenModel):
    """base_images maps a spec base name to its registry repository. Apt sources may use http: base images carry
    no CA bundle before ca-certificates is installed and apt checks every Release signature either way."""

    base_images: dict[str, str]
    apt_sources: tuple[str, ...] = Field(min_length=1)
    guest_agent_wheel: Path
    guest_agent_requirements: Path
    kernel_artifact: str = "guest_kernel"
    rootfs_size_mib: PositiveInt = 8192
    fonts: tuple[str, ...] = ("fonts-dejavu-core", "fonts-liberation2", "fonts-noto-core")
    platform: str = "linux/amd64"
    user: str = "agent"

    @field_validator("apt_sources")
    @classmethod
    def check_snapshot_mirrors(cls, apt_sources: tuple[str, ...]) -> tuple[str, ...]:
        moving = [line for line in apt_sources if not any(host in line for host in SNAPSHOT_MIRROR_HOSTS)]
        if moving:
            raise ValueError(f"apt sources must be dated snapshot mirrors, got {moving}")
        return apt_sources


class WindowsBuildConfig(FrozenModel):
    product_key: str = Field(pattern=r"^[A-Z0-9]{5}(-[A-Z0-9]{5}){4}$")
    kms_host: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.-]+(:[0-9]+)?$")
    office_builds: dict[str, OfficeBuild] = Field(default_factory=dict)
    iso_artifact: str = "windows_server_2025"
    virtio_artifact: str = "virtio_win"
    virtio_driver_os: str = "2k25"
    helper_artifact: str = "rlworld_windows_helper"
    office_tool_artifact: str = "office_deployment_tool"
    image_name: str = "Windows Server 2025 SERVERDATACENTER"
    disk_size_mib: PositiveInt = 81_920
    memory_mib: PositiveInt = 8192
    vcpus: PositiveInt = 4
    time_zone: str = "UTC"
    user: str = "agent"


class BuildConfig(FrozenModel):
    linux: LinuxBuildConfig | None = None
    windows: WindowsBuildConfig | None = None
    trusted_cas: dict[CaName, Path] = Field(default_factory=dict)


@dataclass(frozen=True)
class BuildCommand:
    """Run from the work directory; the environment is merged over the builder's and never recorded."""

    argv: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildPlan:
    files: Mapping[str, bytes]
    copies: Mapping[str, Path]
    downloads: Mapping[str, Artifact]
    commands: tuple[BuildCommand, ...]
    outputs: Mapping[str, str]
    manifest: Mapping[str, Any]


def locale_parts(locale: str) -> tuple[str, str]:
    """Language and territory of a display locale such as en_US."""
    match = LOCALE_PATTERN.match(locale)
    if match is None:
        raise BuildError([f"display locale {locale} must look like en_US"])
    return match.group(1), match.group(2)


def load_artifact_lock(lock_path: Path) -> ArtifactLock:
    return ArtifactLock.model_validate(yaml.safe_load(lock_path.read_text()) or {})


def load_build_config(config_path: Path) -> BuildConfig:
    raw_config = yaml.safe_load(config_path.read_text()) or {}
    base = config_path.parent
    linux = raw_config.get("linux")
    if isinstance(linux, dict):
        raw_config["linux"] = {**linux, **{key: str(base / linux[key]) for key in LINUX_PATH_FIELDS if key in linux}}
    raw_config["trusted_cas"] = {name: str(base / path) for name, path in (raw_config.get("trusted_cas") or {}).items()}
    return BuildConfig.model_validate(raw_config)


def read_trusted_cas(trusted_cas: Mapping[str, Path]) -> dict[str, bytes]:
    """PEM bytes by CA name; anything that is not a CA certificate fails the build."""
    certificates = {}
    problems = []
    for name, certificate_path in sorted(trusted_cas.items()):
        pem = certificate_path.read_bytes()
        try:
            certificate = x509.load_pem_x509_certificate(pem)
            is_authority = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except (ValueError, x509.ExtensionNotFound):
            is_authority = False
        if is_authority:
            certificates[name] = pem
        else:
            problems.append(f"trusted CA {name} at {certificate_path} is not a PEM CA certificate")
    if problems:
        raise BuildError(problems)
    return certificates
