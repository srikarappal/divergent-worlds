import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from rlworld.network import (
    Capture,
    EgressLimits,
    NetworkPlanError,
    build_network_plan,
    captures_due,
    create_certificate_authority,
    egress_proxy_command,
    load_capture_manifest,
    pinned_capture_paths,
    publish_capture,
    record_proxy_command,
    replay_proxy_artifacts,
    replay_proxy_command,
)
from rlworld.specs.models import NetworkSpec

SIDECARS = {"replay_proxy": "172.16.0.53", "egress_proxy": "172.16.0.54"}
WORLD_TIME = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)
REPLAY_FLAGS = ("--set", "upstream_cert=false", "--set", "connection_strategy=lazy", "--set", "server_replay_extra=404",
                "--set", "server_replay_reuse=true", "--set", "server_replay_refresh=false")


def plan_for(internet, allowlist=()):
    network_spec = NetworkSpec(internet=internet, allowlist=list(allowlist))
    return build_network_plan(network_spec, SIDECARS, "clone00007", control_plane_cidrs=("10.0.0.0/16",))


def recorded(tmp_path, name, content):
    path = tmp_path / "recordings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def captures(tmp_path):
    directory = tmp_path / "captures"
    publish_capture(directory, "news.vendor.example", recorded(tmp_path, "news.flows", b"news flows"),
                    WORLD_TIME - timedelta(days=40), refresh_days=30)
    publish_capture(directory, "docs.vendor.example", recorded(tmp_path, "docs.flows", b"docs flows"),
                    WORLD_TIME - timedelta(days=5), refresh_days=7)
    return directory


def test_published_captures_are_pinned_by_digest_in_the_manifest(captures, tmp_path):
    manifest = load_capture_manifest(captures)
    assert [capture.site for capture in manifest.captures] == ["docs.vendor.example", "news.vendor.example"]
    docs = manifest.captures[0]
    digest = hashlib.sha256(b"docs flows").hexdigest()
    assert (docs.file, docs.sha256, docs.refresh_days) == (f"docs.vendor.example_{digest[:12]}.flows", digest, 7)
    assert (captures / docs.file).read_bytes() == b"docs flows"
    assert not (tmp_path / "recordings" / "docs.flows").exists()
    assert json.loads((captures / "captures.json").read_text())["captures"][0]["site"] == "docs.vendor.example"


def test_a_new_recording_replaces_its_sites_capture(captures, tmp_path):
    fresh = publish_capture(captures, "news.vendor.example", recorded(tmp_path, "news2.flows", b"news again"),
                            WORLD_TIME)
    sites = {capture.site: capture for capture in load_capture_manifest(captures).captures}
    assert len(sites) == 2 and sites["news.vendor.example"] == fresh
    assert fresh.sha256 == hashlib.sha256(b"news again").hexdigest()


def test_captures_come_due_after_their_refresh_interval(captures):
    assert [capture.site for capture in captures_due(captures, WORLD_TIME)] == ["news.vendor.example"]
    later = WORLD_TIME + timedelta(days=2)
    assert [capture.site for capture in captures_due(captures, later)] == ["news.vendor.example",
                                                                           "docs.vendor.example"]
    assert captures_due(captures / "nowhere", later) == []
    capture = Capture(site="a.example", file="a.flows", sha256="0" * 64, recorded_at=WORLD_TIME, refresh_days=1)
    assert not capture.due(WORLD_TIME + timedelta(hours=23)) and capture.due(WORLD_TIME + timedelta(days=1))


def test_replay_serves_only_pinned_captures_without_reaching_the_internet(captures):
    authority = create_certificate_authority("world ca", WORLD_TIME)
    artifacts = replay_proxy_artifacts(plan_for("replay_only"), authority, captures)
    assert artifacts.files == {"mitmproxy/mitmproxy-ca.pem": authority.private_key_pem + authority.certificate_pem}
    paths = [str(captures / capture.file) for capture in load_capture_manifest(captures).captures]
    assert artifacts.commands == (("mitmdump", "--listen-host", "172.16.0.53", "--listen-port", "3128", "--set",
                                   "confdir=mitmproxy", *REPLAY_FLAGS, "--server-replay", paths[0],
                                   "--server-replay", paths[1]),)


def test_replay_refuses_captures_that_changed_or_vanished(captures):
    manifest = load_capture_manifest(captures)
    (captures / manifest.captures[0].file).write_bytes(b"edited after recording")
    with pytest.raises(NetworkPlanError, match="changed since it was recorded"):
        pinned_capture_paths(captures)
    (captures / manifest.captures[0].file).unlink()
    with pytest.raises(NetworkPlanError, match="is missing"):
        pinned_capture_paths(captures)


@pytest.mark.parametrize("internet", ["none", "allowlist", "live"])
def test_only_replay_envs_get_a_replay_proxy(internet):
    with pytest.raises(NetworkPlanError, match="has no replay proxy"):
        replay_proxy_command(plan_for(internet, allowlist=["docs.vendor.example"] if internet == "allowlist" else ()),
                             "mitmproxy", [])


def test_recording_streams_every_flow_to_a_new_capture(tmp_path):
    assert record_proxy_command("172.16.0.60", "mitmproxy", tmp_path / "news.flows") == (
        "mitmdump", "--listen-host", "172.16.0.60", "--listen-port", "3128", "--set", "confdir=mitmproxy",
        "--save-stream-file", str(tmp_path / "news.flows"))


def test_published_sites_must_be_hostnames(tmp_path):
    with pytest.raises(NetworkPlanError, match="not a valid hostname"):
        publish_capture(tmp_path / "captures", "../etc", recorded(tmp_path, "x.flows", b"x"), WORLD_TIME)


def test_allowlist_envs_reach_their_hosts_through_the_logging_proxy():
    plan = plan_for("allowlist", allowlist=["status.vendor.example", "docs.vendor.example"])
    assert egress_proxy_command(plan, "env_000042", "/var/log/rlworld/env_000042.jsonl") == (
        "/usr/local/bin/rlworld-egress-proxy", "-listen=172.16.0.54:3128", "-env=env_000042",
        "-allow=docs.vendor.example,status.vendor.example", "-ports=80,443", "-rate=10", "-burst=20",
        "-max-tunnels=64", "-block=169.254.0.0/16,10.0.0.0/16", "-log=/var/log/rlworld/env_000042.jsonl")


def test_live_envs_reach_any_public_host_within_their_limits():
    command = egress_proxy_command(plan_for("live"), "env_7", "/logs/env_7.jsonl",
                                   EgressLimits(requests_per_second=2.5, burst=4, max_tunnels=8), binary="egressproxy")
    assert command[0] == "egressproxy" and "-allow-all" in command
    assert {"-rate=2.5", "-burst=4", "-max-tunnels=8"} <= set(command)
    assert not any(item.startswith("-allow=") for item in command)


@pytest.mark.parametrize("internet", ["none", "replay_only"])
def test_envs_without_live_internet_get_no_egress_proxy(internet):
    with pytest.raises(NetworkPlanError, match="has no egress proxy"):
        egress_proxy_command(plan_for(internet), "env_1", "/logs/env_1.jsonl")
