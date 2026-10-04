"""Runs an image build and records what it produced (DESIGN.md sections 6.2 and 7).

The builder picks the plan for the image's OS and target, writes its files,
copies local inputs, fetches host side downloads checked against the artifact
lock, runs the commands in order and stops at the first failure. manifest.json
then records the image spec and its digest, every pinned artifact and input,
the trusted CAs and the sha256 of every output, which is what the snapshot
builder and later reproductions start from. The vm target builds VM disks; the
dev target builds Linux container images for the Docker dev backend. With
--snapshot the build continues on fleetd's host, which boots it to its readiness
probe and publishes the base snapshot (DESIGN.md section 3, lifecycle step 1).
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
import yaml
from pydantic import ValidationError

from rlworld.images.config import (
    Artifact,
    ArtifactLock,
    BuildConfig,
    BuildError,
    BuildPlan,
    load_artifact_lock,
    load_build_config,
    read_trusted_cas,
)
from rlworld.images.linux import linux_build_plan, linux_dev_build_plan
from rlworld.images.windows import windows_build_plan
from rlworld.sdk.client import FleetClient, FleetError
from rlworld.specs.digest import spec_digest
from rlworld.specs.document import format_validation_errors
from rlworld.specs.models import ImageSpec

CHUNK_BYTES = 1 << 20
MANIFEST_NAME = "manifest.json"
DEFAULT_FLEET_URL = "http://localhost:8700"
SNAPSHOT_TIMEOUT_SECONDS = 1800.0
BuildTarget = Literal["vm", "dev"]


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], working_directory: Path, environment: Mapping[str, str]) -> None: ...


class Fetcher(Protocol):
    def fetch(self, artifact: Artifact, destination: Path) -> None: ...


class SnapshotBuilder(Protocol):
    def build_snapshot(self, build_dir: str, memory_mib: int, vcpus: int) -> dict[str, Any]: ...


class SubprocessRunner:
    def run(self, argv: Sequence[str], working_directory: Path, environment: Mapping[str, str]) -> None:
        completed = subprocess.run(list(argv), cwd=working_directory, env={**os.environ, **environment})
        if completed.returncode != 0:
            raise BuildError([f"{' '.join(argv)} exited with {completed.returncode}"])


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class HttpFetcher:
    """Streams to a partial file and only renames it into place once the sha256 matches the lock."""

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self.http_client = http_client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0))

    def fetch(self, artifact: Artifact, destination: Path) -> None:
        partial = destination.with_name(f"{destination.name}.partial")
        digest = hashlib.sha256()
        with self.http_client.stream("GET", artifact.url) as response:
            if response.status_code != 200:
                raise BuildError([f"{artifact.url} answered {response.status_code}"])
            with partial.open("wb") as output:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != artifact.sha256:
            partial.unlink()
            raise BuildError([f"{artifact.url} does not match its sha256 in the artifact lock"])
        partial.replace(destination)


def build_plan(image: ImageSpec, config: BuildConfig, lock: ArtifactLock, trusted_cas: Mapping[str, bytes],
               target: BuildTarget = "vm") -> BuildPlan:
    if target == "dev" and image.os != "linux":
        raise BuildError([f"dev containers run linux images only, not {image.os}"])
    if image.os == "linux":
        if config.linux is None:
            raise BuildError(["the build config has no linux section"])
        plan_for_target = linux_dev_build_plan if target == "dev" else linux_build_plan
        return plan_for_target(image, config.linux, lock, trusted_cas)
    if image.os == "windows":
        if config.windows is None:
            raise BuildError(["the build config has no windows section"])
        return windows_build_plan(image, config.windows, lock, trusted_cas)
    raise BuildError([f"there is no image pipeline for {image.os} guests yet"])


def utc_now() -> datetime:
    return datetime.now(UTC)


class ImageBuilder:
    def __init__(self, runner: CommandRunner, fetcher: Fetcher, clock: Callable[[], datetime] = utc_now) -> None:
        self.runner = runner
        self.fetcher = fetcher
        self.clock = clock

    def fetch_once(self, artifact: Artifact, destination: Path) -> None:
        """A download already in place with the locked sha256 is reused across rebuilds."""
        if destination.exists() and file_sha256(destination) == artifact.sha256:
            return
        self.fetcher.fetch(artifact, destination)

    def build(self, image: ImageSpec, config: BuildConfig, lock: ArtifactLock, work_directory: Path,
              target: BuildTarget = "vm") -> dict[str, Any]:
        trusted_cas = read_trusted_cas(config.trusted_cas)
        plan = build_plan(image, config, lock, trusted_cas, target)
        for relative_path, content in plan.files.items():
            destination = work_directory / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        for relative_path, source in plan.copies.items():
            destination = work_directory / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        for relative_path, artifact in plan.downloads.items():
            destination = work_directory / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.fetch_once(artifact, destination)
        for command in plan.commands:
            self.runner.run(command.argv, work_directory, command.environment)
        missing = [path for path in plan.outputs.values() if not (work_directory / path).is_file()]
        if missing:
            raise BuildError([f"the build finished without producing {path}" for path in missing])
        manifest = {
            "image": image.model_dump(mode="json"),
            "image_digest": spec_digest(image),
            "target": target,
            "built_at": self.clock().isoformat(),
            "inputs": {path: file_sha256(work_directory / path) for path in plan.copies},
            "trusted_cas": {name: hashlib.sha256(pem).hexdigest() for name, pem in trusted_cas.items()},
            "outputs": {role: {"path": path, "sha256": file_sha256(work_directory / path),
                               "bytes": (work_directory / path).stat().st_size}
                        for role, path in plan.outputs.items()},
            **plan.manifest,
        }
        (work_directory / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return manifest


def build_snapshot(fleet: SnapshotBuilder, work_directory: Path, manifest: Mapping[str, Any], memory_mib: int,
                   vcpus: int) -> dict[str, Any]:
    """fleetd boots a vm build on its host and publishes the base snapshot; the summary names it for the build."""
    if manifest["target"] != "vm":
        raise BuildError([f"only vm builds are snapshotted, {manifest['image']['id']} is a {manifest['target']} build"])
    snapshot = fleet.build_snapshot(str(work_directory.resolve()), memory_mib, vcpus)
    if snapshot["image_digest"] != manifest["image_digest"]:
        raise BuildError([f"fleetd snapshotted image {snapshot['image_digest']}, not {manifest['image_digest']}"])
    return {"digest": snapshot["digest"], "image": snapshot["image"], "image_digest": snapshot["image_digest"],
            "pool": snapshot["compatibility"]["pool"], "memory_mib": snapshot["memory_mib"],
            "vcpus": snapshot["vcpus"]}


def load_image_spec(spec_path: Path) -> ImageSpec:
    """The image layer of a spec document, or a file holding only that layer."""
    raw_spec = yaml.safe_load(spec_path.read_text())
    if not isinstance(raw_spec, dict) or not isinstance(raw_spec.get("image"), dict):
        raise BuildError([f"{spec_path} has no image layer"])
    try:
        return ImageSpec.model_validate(raw_spec["image"])
    except ValidationError as error:
        raise BuildError([f"image.{problem}" for problem in format_validation_errors(error)]) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rlworld-build-image",
                                     description="Build the image layer of a spec into VM disk artifacts.")
    parser.add_argument("spec", type=Path, help="spec document or image layer YAML")
    parser.add_argument("--config", type=Path, required=True, help="build config YAML")
    parser.add_argument("--lock", type=Path, required=True, help="artifact lock YAML")
    parser.add_argument("--work-dir", type=Path, required=True, help="where the build writes its files and outputs")
    parser.add_argument("--target", choices=("vm", "dev"), default="vm",
                        help="vm disks for the pools, or a container image for the Docker dev backend")
    parser.add_argument("--snapshot", action="store_true",
                        help="then have fleetd on this host boot the build and publish its base snapshot")
    parser.add_argument("--fleetd-url", default=os.environ.get("RLWORLD_FLEET_URL", DEFAULT_FLEET_URL))
    parser.add_argument("--memory-mib", type=int, default=None, help="memory of the pool's leases")
    parser.add_argument("--vcpus", type=int, default=None, help="vCPUs of the pool's leases")
    return parser


def main(argv: Sequence[str] | None = None, builder: ImageBuilder | None = None,
         fleet: SnapshotBuilder | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.snapshot and (arguments.target != "vm" or not arguments.memory_mib or not arguments.vcpus):
        parser.error("--snapshot needs the vm target with --memory-mib and --vcpus")
    try:
        image = load_image_spec(arguments.spec)
        config = load_build_config(arguments.config)
        lock = load_artifact_lock(arguments.lock)
        arguments.work_dir.mkdir(parents=True, exist_ok=True)
        manifest = (builder or ImageBuilder(SubprocessRunner(), HttpFetcher())).build(
            image, config, lock, arguments.work_dir, arguments.target)
        summary: Any = manifest["outputs"]
        if arguments.snapshot:
            snapshotter = fleet or FleetClient(arguments.fleetd_url, timeout_seconds=SNAPSHOT_TIMEOUT_SECONDS)
            summary = {"outputs": manifest["outputs"], "snapshot": build_snapshot(
                snapshotter, arguments.work_dir, manifest, arguments.memory_mib, arguments.vcpus)}
    except (BuildError, ValidationError, OSError, httpx.HTTPError, FleetError) as error:
        print(f"image build failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
