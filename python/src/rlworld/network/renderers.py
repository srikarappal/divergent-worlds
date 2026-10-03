"""Render a network plan into files and commands the node agent applies inside the env namespace.

DNS is dnsmasq, egress is an nftables forward chain that drops by default, and
faults are a netem qdisc on the guest interface. Commands are argv lists so
nothing ever passes through a shell.
"""

from dataclasses import dataclass

from rlworld.network.plan import PROXY_PORT, NetworkPlan

UPSTREAM_RESOLVER = "1.1.1.1"
LIVE_NEW_CONNECTIONS_PER_SECOND = 50


@dataclass(frozen=True)
class NetworkArtifacts:
    files: dict[str, bytes]
    commands: tuple[tuple[str, ...], ...]


def render_dnsmasq_config(plan: NetworkPlan) -> str:
    """Overrides first; then the mode decides what every other name resolves to."""
    lines = [
        "no-hosts",
        "no-resolv",
        f"listen-address={plan.gateway_address}",
        "bind-interfaces",
        "log-queries",
    ]
    lines.extend(f"address=/{hostname}/{address}" for hostname, address in plan.dns_overrides.items())
    if plan.internet_mode == "none":
        lines.append("address=/#/")
    elif plan.internet_mode == "replay_only":
        lines.append(f"address=/#/{plan.proxy_address}")
    elif plan.internet_mode == "allowlist":
        lines.extend(f"server=/{hostname}/{UPSTREAM_RESOLVER}" for hostname in plan.allowlist)
        lines.append("address=/#/")
    else:
        lines.append(f"server={UPSTREAM_RESOLVER}")
    return "\n".join(lines) + "\n"


def render_nftables_ruleset(plan: NetworkPlan) -> str:
    """Default drop for traffic leaving the env; only the proxy is reachable, never metadata or the control plane."""
    blocked_elements = ", ".join(plan.blocked_cidrs)
    forward_rules = [
        "ct state established,related accept",
        "ip daddr @always_blocked drop",
        f"ip saddr {plan.guest_address} ip daddr {plan.gateway_address} udp dport 53 accept",
        f"ip saddr {plan.guest_address} ip daddr {plan.gateway_address} tcp dport 53 accept",
    ]
    if plan.proxy_address is not None:
        if plan.internet_mode == "live":
            forward_rules.append(
                f"ip daddr {plan.proxy_address} ct state new limit rate over "
                f"{LIVE_NEW_CONNECTIONS_PER_SECOND}/second drop"
            )
        forward_rules.append(f"ip saddr {plan.guest_address} ip daddr {plan.proxy_address} tcp dport {PROXY_PORT} accept")
    forward_rules.append('log prefix "rlworld egress drop: " drop')
    rule_lines = "\n".join(f"    {rule}" for rule in forward_rules)
    return (
        "table inet rlworld_env {\n"
        "  set always_blocked {\n"
        "    type ipv4_addr\n"
        "    flags interval\n"
        f"    elements = {{ {blocked_elements} }}\n"
        "  }\n"
        "  chain forward {\n"
        "    type filter hook forward priority 0; policy drop;\n"
        f"{rule_lines}\n"
        "  }\n"
        "}\n"
    )


def netem_command(plan: NetworkPlan) -> tuple[str, ...]:
    if plan.faults is None:
        return ("tc", "qdisc", "del", "dev", plan.guest_interface, "root")
    return (
        "tc", "qdisc", "replace", "dev", plan.guest_interface, "root", "netem",
        "delay", f"{plan.faults.latency_ms}ms",
        "loss", f"{plan.faults.packet_loss_percent}%",
    )


def render_network_artifacts(plan: NetworkPlan, ca_certificate_pem: bytes,
                             server_certificates: dict[str, tuple[bytes, bytes]]) -> NetworkArtifacts:
    """Everything the node agent writes and runs for one env; certificates are keyed by hostname."""
    files = {
        "dnsmasq.conf": render_dnsmasq_config(plan).encode("utf-8"),
        "nftables.conf": render_nftables_ruleset(plan).encode("utf-8"),
        "ca.pem": ca_certificate_pem,
    }
    for hostname, (certificate_pem, private_key_pem) in sorted(server_certificates.items()):
        files[f"certs/{hostname}.pem"] = certificate_pem
        files[f"certs/{hostname}.key"] = private_key_pem
    commands = (
        ("nft", "-f", "nftables.conf"),
        ("dnsmasq", "--conf-file=dnsmasq.conf"),
        netem_command(plan),
    )
    return NetworkArtifacts(files=files, commands=commands)
