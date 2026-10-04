"""Proxy sidecars of the network world (DESIGN.md sections 6.6 and 7).

Sites that cannot be cloned are served by the record and replay proxy,
mitmproxy's server replay over pinned captures. Replay never reaches the
internet: a request without a capture gets a 404, captured dates stay as
recorded and every capture file must match the digest its manifest pins, so
a page replays the same in every episode. Record mode writes a fresh capture
of a site through the world's CA when the manifest says the site is due.
Live internet, eval only, leaves through the egress proxy, which checks every
request against the env's allowlist, rate limits it and logs it.
"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from operator import attrgetter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from rlworld.network.certificates import CertificateAuthority
from rlworld.network.plan import HOSTNAME_PATTERN, PROXY_PORT, NetworkPlan, NetworkPlanError
from rlworld.network.renderers import NetworkArtifacts

MITMDUMP = "mitmdump"
EGRESS_PROXY_BINARY = "/usr/local/bin/rlworld-egress-proxy"
MITMPROXY_CONFDIR = "mitmproxy"
MITMPROXY_CA_FILE = "mitmproxy-ca.pem"
CAPTURE_MANIFEST = "captures.json"
WEB_PORTS = (80, 443)
REPLAY_OPTIONS = (
    "upstream_cert=false",
    "connection_strategy=lazy",
    "server_replay_extra=404",
    "server_replay_reuse=true",
    "server_replay_refresh=false",
)


class CaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Capture(CaptureModel):
    """One site's recorded traffic: the flows file beside the manifest, pinned by its digest."""

    site: str
    file: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.flows$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recorded_at: datetime
    refresh_days: PositiveInt = 30

    def due(self, now: datetime) -> bool:
        return self.recorded_at + timedelta(days=self.refresh_days) <= now


class CaptureManifest(CaptureModel):
    captures: tuple[Capture, ...] = ()


@dataclass(frozen=True)
class EgressLimits:
    requests_per_second: float = 10.0
    burst: int = 20
    max_tunnels: int = 64


def load_capture_manifest(directory: Path) -> CaptureManifest:
    manifest_path = directory / CAPTURE_MANIFEST
    if not manifest_path.is_file():
        return CaptureManifest()
    return CaptureManifest.model_validate_json(manifest_path.read_text())


def file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def pinned_capture_paths(directory: Path) -> list[Path]:
    """Every capture file the manifest lists, refused unless it is exactly the file that was recorded."""
    paths = []
    for capture in load_capture_manifest(directory).captures:
        path = directory / capture.file
        if not path.is_file():
            raise NetworkPlanError(f"capture {capture.file} of {capture.site} is missing")
        if file_sha256(path) != capture.sha256:
            raise NetworkPlanError(f"capture {capture.file} of {capture.site} changed since it was recorded")
        paths.append(path)
    return paths


def captures_due(directory: Path, now: datetime) -> list[Capture]:
    """Captures whose refresh interval has passed, oldest first, for the next recording round."""
    due = [capture for capture in load_capture_manifest(directory).captures if capture.due(now)]
    return sorted(due, key=attrgetter("recorded_at"))


def publish_capture(directory: Path, site: str, recorded_path: Path, recorded_at: datetime,
                    refresh_days: int = 30) -> Capture:
    """Pins a freshly recorded flows file under its digest and makes it the site's capture in the manifest."""
    if not HOSTNAME_PATTERN.match(site):
        raise NetworkPlanError(f"{site!r} is not a valid hostname")
    digest = file_sha256(recorded_path)
    capture = Capture(site=site, file=f"{site}_{digest[:12]}.flows", sha256=digest, recorded_at=recorded_at,
                      refresh_days=refresh_days)
    directory.mkdir(parents=True, exist_ok=True)
    os.replace(recorded_path, directory / capture.file)
    others = [existing for existing in load_capture_manifest(directory).captures if existing.site != site]
    manifest = CaptureManifest(captures=tuple(sorted([*others, capture], key=attrgetter("site"))))
    descriptor, temporary_path = tempfile.mkstemp(dir=directory, prefix=".manifest_")
    with os.fdopen(descriptor, "w") as handle:
        handle.write(manifest.model_dump_json(indent=2))
    os.replace(temporary_path, directory / CAPTURE_MANIFEST)
    return capture


def mitmproxy_ca(authority: CertificateAuthority) -> bytes:
    """mitmproxy reads its CA as one PEM holding the private key and then the certificate."""
    return authority.private_key_pem + authority.certificate_pem


def replay_proxy_command(plan: NetworkPlan, confdir: str, capture_paths: list[Path]) -> tuple[str, ...]:
    if plan.internet_mode != "replay_only" or plan.proxy_address is None:
        raise NetworkPlanError(f"{plan.internet_mode} internet has no replay proxy")
    options = tuple(item for option in (f"confdir={confdir}", *REPLAY_OPTIONS) for item in ("--set", option))
    captures = tuple(item for path in capture_paths for item in ("--server-replay", str(path)))
    return (MITMDUMP, "--listen-host", plan.proxy_address, "--listen-port", str(PROXY_PORT), *options, *captures)


def replay_proxy_artifacts(plan: NetworkPlan, authority: CertificateAuthority,
                           capture_directory: Path) -> NetworkArtifacts:
    """The replay sidecar's files and command; the CA key stays in the sidecar, outside every VM."""
    return NetworkArtifacts(
        files={f"{MITMPROXY_CONFDIR}/{MITMPROXY_CA_FILE}": mitmproxy_ca(authority)},
        commands=(replay_proxy_command(plan, MITMPROXY_CONFDIR, pinned_capture_paths(capture_directory)),),
    )


def record_proxy_command(listen_address: str, confdir: str, output_path: Path) -> tuple[str, ...]:
    """A recording session's proxy: live upstream, every flow streamed to output_path."""
    return (MITMDUMP, "--listen-host", listen_address, "--listen-port", str(PROXY_PORT), "--set",
            f"confdir={confdir}", "--save-stream-file", str(output_path))


def egress_proxy_command(plan: NetworkPlan, env_id: str, log_path: str, limits: EgressLimits = EgressLimits(),
                         binary: str = EGRESS_PROXY_BINARY) -> tuple[str, ...]:
    """The egress sidecar for an allowlist or live env, limited to web ports and logging every request."""
    if plan.internet_mode not in ("allowlist", "live") or plan.proxy_address is None:
        raise NetworkPlanError(f"{plan.internet_mode} internet has no egress proxy")
    hosts = "-allow-all" if plan.internet_mode == "live" else f"-allow={','.join(plan.allowlist)}"
    return (binary, f"-listen={plan.proxy_address}:{PROXY_PORT}", f"-env={env_id}", hosts,
            f"-ports={','.join(str(port) for port in WEB_PORTS)}", f"-rate={limits.requests_per_second:g}",
            f"-burst={limits.burst}", f"-max-tunnels={limits.max_tunnels}", f"-block={','.join(plan.blocked_cidrs)}",
            f"-log={log_path}")
