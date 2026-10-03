"""Runs an image build and records what it produced (DESIGN.md sections 6.2 and 7).

The builder picks the plan for the image's OS, writes its files, copies local
inputs, fetches host side downloads checked against the artifact lock, runs
the commands in order and stops at the first failure. manifest.json then
records the image spec and its digest, every pinned artifact and input, the
trusted CAs and the sha256 of every output, which is what the snapshot builder
and later reproductions start from.
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
from typing import Any, Protocol

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
from rlworld.images.linux import linux_build_plan
from rlworld.images.windows import windows_build_plan
from rlworld.specs.digest import spec_digest
from rlworld.specs.document import format_validation_errors
from rlworld.specs.models import ImageSpec

CHUNK_BYTES = 1 << 20
MANIFEST_NAME = "manifest.json"


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], working_directory: Path, environment: Mapping[str, str]) -> None: ...


class Fetcher(Protocol):
    def fetch(self, artifact: Artifact, destination: Path) -> None: ...


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


def build_plan(image: ImageSpec, config: BuildConfig, lock: ArtifactLock,
               trusted_cas: Mapping[str, bytes]) -> BuildPlan:
    if image.os == "linux":
        if config.linux is None:
            raise BuildError(["the build config has no linux section"])
        return linux_build_plan(image, config.linux, lock, trusted_cas)
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

    def build(self, image: ImageSpec, config: BuildConfig, lock: ArtifactLock, work_directory: Path) -> dict[str, Any]:
        trusted_cas = read_trusted_cas(config.trusted_cas)
        plan = build_plan(image, config, lock, trusted_cas)
        for relative_path, content in plan.files.items():
            target = work_directory / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for relative_path, source in plan.copies.items():
            target = work_directory / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for relative_path, artifact in plan.downloads.items():
            target = work_directory / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            self.fetch_once(artifact, target)
        for command in plan.commands:
            self.runner.run(command.argv, work_directory, command.environment)
        missing = [path for path in plan.outputs.values() if not (work_directory / path).is_file()]
        if missing:
            raise BuildError([f"the build finished without producing {path}" for path in missing])
        manifest = {
            "image": image.model_dump(mode="json"),
            "image_digest": spec_digest(image),
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        image = load_image_spec(arguments.spec)
        config = load_build_config(arguments.config)
        lock = load_artifact_lock(arguments.lock)
        arguments.work_dir.mkdir(parents=True, exist_ok=True)
        manifest = ImageBuilder(SubprocessRunner(), HttpFetcher()).build(image, config, lock, arguments.work_dir)
    except (BuildError, ValidationError, OSError, httpx.HTTPError) as error:
        print(f"image build failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(manifest["outputs"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
