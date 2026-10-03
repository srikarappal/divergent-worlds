"""Per env network plan (DESIGN.md sections 6.1 and 6.6).

Every clone gets its own namespace with the same guest address behind NAT.
The plan resolves DNS overrides to sidecar addresses, picks the egress mode,
lists the networks that are always blocked and samples concrete fault values
for this episode from the task's ranges.
"""

import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from rlworld.specs.models import NetworkFaultSpec, NetworkSpec

GUEST_ADDRESS = "172.16.0.2"
GATEWAY_ADDRESS = "172.16.0.1"
ENV_SUBNET = "172.16.0.0/24"
ALWAYS_BLOCKED_CIDRS = ("169.254.0.0/16",)
REPLAY_PROXY_SIDECAR = "replay_proxy"
EGRESS_PROXY_SIDECAR = "egress_proxy"
PROXY_PORT = 3128
HOSTNAME_PATTERN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class NetworkPlanError(ValueError):
    """A network spec that cannot be wired with the sidecars this env has."""


@dataclass(frozen=True)
class ConcreteFaults:
    latency_ms: int
    packet_loss_percent: float


@dataclass(frozen=True)
class NetworkPlan:
    namespace: str
    guest_interface: str
    internet_mode: Literal["none", "replay_only", "allowlist", "live"]
    dns_overrides: dict[str, str]
    allowlist: tuple[str, ...]
    blocked_cidrs: tuple[str, ...]
    proxy_address: str | None
    faults: ConcreteFaults | None
    guest_address: str = GUEST_ADDRESS
    gateway_address: str = GATEWAY_ADDRESS
    env_subnet: str = ENV_SUBNET


def sample_faults(fault_spec: NetworkFaultSpec | None, seed: int) -> ConcreteFaults | None:
    """Concrete latency and loss for one episode, drawn from the task ranges with the episode seed."""
    if fault_spec is None:
        return None
    random_generator = random.Random(seed)
    latency_ms = random_generator.randint(*fault_spec.latency_ms)
    packet_loss_percent = round(random_generator.uniform(*fault_spec.packet_loss) * 100, 3)
    if latency_ms == 0 and packet_loss_percent == 0:
        return None
    return ConcreteFaults(latency_ms, packet_loss_percent)


def build_network_plan(
    network_spec: NetworkSpec,
    sidecar_addresses: Mapping[str, str],
    namespace: str,
    guest_interface: str = "tap0",
    fault_spec: NetworkFaultSpec | None = None,
    episode_seed: int = 0,
    control_plane_cidrs: Sequence[str] = (),
) -> NetworkPlan:
    resolved_overrides = {}
    for hostname, sidecar_name in sorted(network_spec.dns_overrides.items()):
        if not HOSTNAME_PATTERN.match(hostname):
            raise NetworkPlanError(f"{hostname!r} is not a valid hostname")
        if sidecar_name not in sidecar_addresses:
            raise NetworkPlanError(f"dns override {hostname} points at unknown sidecar {sidecar_name}")
        resolved_overrides[hostname] = sidecar_addresses[sidecar_name]
    proxy_address = None
    if network_spec.internet == "replay_only":
        proxy_address = sidecar_addresses.get(REPLAY_PROXY_SIDECAR)
        if proxy_address is None:
            raise NetworkPlanError("replay_only internet needs a replay_proxy sidecar")
    elif network_spec.internet in ("allowlist", "live"):
        proxy_address = sidecar_addresses.get(EGRESS_PROXY_SIDECAR)
        if proxy_address is None:
            raise NetworkPlanError(f"{network_spec.internet} internet needs an egress_proxy sidecar")
    invalid_allowlist = [hostname for hostname in network_spec.allowlist if not HOSTNAME_PATTERN.match(hostname)]
    if invalid_allowlist:
        raise NetworkPlanError(f"invalid allowlist hostnames {invalid_allowlist}")
    return NetworkPlan(
        namespace=namespace,
        guest_interface=guest_interface,
        internet_mode=network_spec.internet,
        dns_overrides=resolved_overrides,
        allowlist=tuple(sorted(network_spec.allowlist)),
        blocked_cidrs=(*ALWAYS_BLOCKED_CIDRS, *control_plane_cidrs),
        proxy_address=proxy_address,
        faults=sample_faults(fault_spec, episode_seed),
    )
