import hashlib
import json
import sys
from datetime import datetime, timezone

import httpx
import pytest
from support.image_fakes import (
    LOCK,
    RecordingFetcher,
    RecordingRunner,
    linux_config,
    linux_image,
    windows_image,
    world_ca_pem,
)

from rlworld.images import (
    Artifact,
    BuildConfig,
    BuildError,
    HttpFetcher,
    ImageBuilder,
    SubprocessRunner,
    build_plan,
    load_image_spec,
)
from rlworld.images.builder import main
from rlworld.specs.digest import spec_digest

BUILT_AT = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
KERNEL = b"kernel bytes"
LOCK_WITH_REAL_KERNEL = LOCK.model_copy(update={"artifacts": {
    **LOCK.artifacts,
    "guest_kernel": Artifact(url="https://kernels.example/vmlinux-6.1", sha256=hashlib.sha256(KERNEL).hexdigest()),
}})


def fixed_clock():
    return BUILT_AT


def linux_inputs(tmp_path):
    wheel = tmp_path / "dist" / "rlworld-0.1.0-py3-none-any.whl"
    requirements = tmp_path / "dist" / "guest.txt"
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel")
    requirements.write_text("pillow==11.0.0 --hash=sha256:abc\n")
    return BuildConfig(linux=linux_config(guest_agent_wheel=wheel, guest_agent_requirements=requirements))


def test_builder_runs_the_plan_and_records_a_manifest(tmp_path):
    config = linux_inputs(tmp_path)
    runner = RecordingRunner(creates={"mke2fs": {"rootfs.ext4": b"ext4 image"}})
    fetcher = RecordingFetcher(KERNEL)
    work = tmp_path / "work"
    manifest = ImageBuilder(runner, fetcher, clock=fixed_clock).build(linux_image(), config, LOCK_WITH_REAL_KERNEL,
                                                                     work)
    assert (work / "context" / "Dockerfile").read_text().startswith("# syntax=docker/dockerfile:1\n")
    assert (work / "context" / "inputs" / "rlworld-0.1.0-py3-none-any.whl").read_bytes() == b"wheel"
    assert (work / "vmlinux").read_bytes() == KERNEL
    assert [command[0][0] for command in runner.commands] == ["docker", "mke2fs", "rm"]
    assert all(command[1] == work for command in runner.commands)
    assert fetcher.fetched == [("https://kernels.example/vmlinux-6.1", work / "vmlinux")]
    assert manifest["image_digest"] == spec_digest(linux_image())
    assert manifest["built_at"] == BUILT_AT.isoformat()
    assert manifest["outputs"]["rootfs"] == {"path": "rootfs.ext4", "sha256": hashlib.sha256(b"ext4 image").hexdigest(),
                                             "bytes": len(b"ext4 image")}
    assert manifest["outputs"]["kernel"]["sha256"] == hashlib.sha256(KERNEL).hexdigest()
    assert manifest["inputs"]["context/inputs/rlworld-0.1.0-py3-none-any.whl"] == hashlib.sha256(b"wheel").hexdigest()
    assert manifest["base_image"].startswith("docker.io/library/ubuntu@sha256:")
    assert manifest["trusted_cas"] == {}
    assert json.loads((work / "manifest.json").read_text()) == manifest


def test_builder_records_trusted_cas_and_refuses_bad_ones_before_running(tmp_path):
    pem = world_ca_pem()
    ca_path = tmp_path / "world.pem"
    ca_path.write_bytes(pem)
    config = linux_inputs(tmp_path).model_copy(update={"trusted_cas": {"world_ca": ca_path}})
    runner = RecordingRunner(creates={"mke2fs": {"rootfs.ext4": b"ext4"}})
    manifest = ImageBuilder(runner, RecordingFetcher(KERNEL)).build(linux_image(), config, LOCK_WITH_REAL_KERNEL,
                                                                   tmp_path / "work")
    assert manifest["trusted_cas"] == {"world_ca": hashlib.sha256(pem).hexdigest()}
    ca_path.write_bytes(b"")
    refused = RecordingRunner()
    with pytest.raises(BuildError, match="trusted CA world_ca"):
        ImageBuilder(refused, RecordingFetcher(KERNEL)).build(linux_image(), config, LOCK_WITH_REAL_KERNEL,
                                                              tmp_path / "refused")
    assert refused.commands == [] and not (tmp_path / "refused").exists()


def test_downloads_already_in_place_are_reused(tmp_path):
    config = linux_inputs(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / "vmlinux").write_bytes(KERNEL)
    fetcher = RecordingFetcher(KERNEL)
    builder = ImageBuilder(RecordingRunner(creates={"mke2fs": {"rootfs.ext4": b"ext4"}}), fetcher, clock=fixed_clock)
    builder.build(linux_image(), config, LOCK_WITH_REAL_KERNEL, work)
    assert fetcher.fetched == []
    (work / "vmlinux").write_bytes(b"stale kernel")
    builder.build(linux_image(), config, LOCK_WITH_REAL_KERNEL, work)
    assert len(fetcher.fetched) == 1 and (work / "vmlinux").read_bytes() == KERNEL


def test_builds_that_produce_nothing_fail(tmp_path):
    with pytest.raises(BuildError, match="the build finished without producing rootfs.ext4"):
        ImageBuilder(RecordingRunner(), RecordingFetcher(KERNEL)).build(linux_image(), linux_inputs(tmp_path),
                                                                        LOCK_WITH_REAL_KERNEL, tmp_path / "work")


def test_dev_builds_record_the_container_image(tmp_path):
    runner = RecordingRunner(creates={"docker": {"image.id": b"sha256:1234"}})
    fetcher = RecordingFetcher(KERNEL)
    manifest = ImageBuilder(runner, fetcher, clock=fixed_clock).build(linux_image(), linux_inputs(tmp_path), LOCK,
                                                                     tmp_path / "work", target="dev")
    assert manifest["target"] == "dev" and fetcher.fetched == []
    assert manifest["outputs"]["image_id"]["sha256"] == hashlib.sha256(b"sha256:1234").hexdigest()
    assert manifest["image_tag"].startswith("rlworld-dev/noble_browsers_v1:")
    assert [command[0][:2] for command in runner.commands] == [("docker", "build")]
    with pytest.raises(BuildError, match="dev containers run linux images only"):
        build_plan(windows_image(), BuildConfig(), LOCK, {}, target="dev")


def test_plans_need_their_os_section():
    with pytest.raises(BuildError, match="no linux section"):
        build_plan(linux_image(), BuildConfig(), LOCK, {})
    with pytest.raises(BuildError, match="no windows section"):
        build_plan(windows_image(), BuildConfig(), LOCK, {})
    android = linux_image(os="android", pool={"vmm": "cuttlefish", "cpu_model": "host"})
    with pytest.raises(BuildError, match="no image pipeline for android guests"):
        build_plan(android, BuildConfig(), LOCK, {})


class StaticTransport(httpx.BaseTransport):
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content
        self.requests = []

    def handle_request(self, request):
        self.requests.append(str(request.url))
        return httpx.Response(self.status_code, content=self.content)


def test_http_fetcher_only_keeps_downloads_matching_the_lock(tmp_path):
    artifact = Artifact(url="https://kernels.example/vmlinux", sha256=hashlib.sha256(KERNEL).hexdigest())
    destination = tmp_path / "vmlinux"
    HttpFetcher(httpx.Client(transport=StaticTransport(200, KERNEL))).fetch(artifact, destination)
    assert destination.read_bytes() == KERNEL
    tampered = tmp_path / "tampered"
    with pytest.raises(BuildError, match="does not match its sha256"):
        HttpFetcher(httpx.Client(transport=StaticTransport(200, b"evil"))).fetch(artifact, tampered)
    assert list(tmp_path.iterdir()) == [destination]
    with pytest.raises(BuildError, match="answered 404"):
        HttpFetcher(httpx.Client(transport=StaticTransport(404, b""))).fetch(artifact, tmp_path / "missing")


def test_subprocess_runner_merges_the_environment_and_fails_loudly(tmp_path):
    script = "import os, pathlib; pathlib.Path('seen.txt').write_text(os.environ['RLWORLD_TEST_VALUE'])"
    SubprocessRunner().run([sys.executable, "-c", script], tmp_path, {"RLWORLD_TEST_VALUE": "from the plan"})
    assert (tmp_path / "seen.txt").read_text() == "from the plan"
    with pytest.raises(BuildError, match="exited with 3"):
        SubprocessRunner().run([sys.executable, "-c", "raise SystemExit(3)"], tmp_path, {})


def test_image_specs_load_from_documents_or_image_layers(tmp_path):
    document = tmp_path / "experiment.yaml"
    document.write_text(json.dumps({"experiment": {"name": "x"}, "image": linux_image().model_dump(mode="json")}))
    assert load_image_spec(document) == linux_image()
    missing = tmp_path / "world_only.yaml"
    missing.write_text("world: {generator: worlds.mid_market_company, seed: 1}\n")
    with pytest.raises(BuildError, match="has no image layer"):
        load_image_spec(missing)
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("image: {id: Bad-Id}\n")
    with pytest.raises(BuildError) as raised:
        load_image_spec(invalid)
    assert all(problem.startswith("image.") for problem in raised.value.problems)


def test_cli_reports_failures_without_a_traceback(tmp_path, capsys):
    spec = tmp_path / "image.yaml"
    spec.write_text("world: {}\n")
    exit_code = main([str(spec), "--config", str(tmp_path / "build.yaml"), "--lock", str(tmp_path / "lock.yaml"),
                      "--work-dir", str(tmp_path / "work")])
    assert exit_code == 1
    assert "image build failed:" in capsys.readouterr().err
