from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from rlworld.network import (
    NetworkPlanError,
    build_network_plan,
    create_certificate_authority,
    issue_server_certificate,
    netem_command,
    render_dnsmasq_config,
    render_network_artifacts,
    render_nftables_ruleset,
    sample_faults,
)
from rlworld.specs import parse_spec_document
from rlworld.specs.models import NetworkFaultSpec, NetworkSpec

SIDECARS = {"crm_clone": "172.16.0.10", "mail_sidecar": "172.16.0.11", "replay_proxy": "172.16.0.53",
            "egress_proxy": "172.16.0.54"}
WORLD_TIME = datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)


def plan_for(internet="none", allowlist=(), overrides=None, faults=None, seed=0):
    network_spec = NetworkSpec(
        dns_overrides=overrides if overrides is not None else {"crm.example.com": "crm_clone"},
        trusted_ca="world_ca",
        internet=internet,
        allowlist=list(allowlist),
    )
    return build_network_plan(network_spec, SIDECARS, "env_0001", fault_spec=faults, episode_seed=seed,
                              control_plane_cidrs=("10.0.0.0/16",))


def test_the_example_world_wires_its_overrides(example_payload):
    document = parse_spec_document(example_payload)
    plan = build_network_plan(document.world.network, SIDECARS, "env_0001", fault_spec=document.task.network_faults)
    assert plan.dns_overrides == {"crm.example.com": "172.16.0.10", "mail.example.com": "172.16.0.11"}
    assert (plan.internet_mode, plan.proxy_address) == ("replay_only", "172.16.0.53")
    assert plan.blocked_cidrs[0] == "169.254.0.0/16"


@pytest.mark.parametrize(
    ("internet", "sidecars", "fragment"),
    [
        ("replay_only", {"crm_clone": "172.16.0.10"}, "replay_proxy"),
        ("live", {"crm_clone": "172.16.0.10"}, "egress_proxy"),
        ("none", {}, "unknown sidecar crm_clone"),
    ],
)
def test_missing_sidecars_are_reported(internet, sidecars, fragment):
    network_spec = NetworkSpec(dns_overrides={"crm.example.com": "crm_clone"}, trusted_ca="world_ca", internet=internet)
    with pytest.raises(NetworkPlanError) as raised:
        build_network_plan(network_spec, sidecars, "env_0001")
    assert fragment in str(raised.value)


def test_dns_per_internet_mode():
    no_internet = render_dnsmasq_config(plan_for("none"))
    assert "address=/crm.example.com/172.16.0.10" in no_internet
    assert no_internet.rstrip().endswith("address=/#/")
    assert render_dnsmasq_config(plan_for("replay_only")).rstrip().endswith("address=/#/172.16.0.53")
    allowlisted = render_dnsmasq_config(plan_for("allowlist", allowlist=["docs.vendor.com"]))
    assert "server=/docs.vendor.com/1.1.1.1" in allowlisted
    assert allowlisted.rstrip().endswith("address=/#/")
    assert "server=1.1.1.1" in render_dnsmasq_config(plan_for("live"))


def test_egress_drops_by_default_and_blocks_metadata():
    offline_rules = render_nftables_ruleset(plan_for("none"))
    assert "policy drop;" in offline_rules
    assert "elements = { 169.254.0.0/16, 10.0.0.0/16 }" in offline_rules
    assert "tcp dport 3128" not in offline_rules
    replay_rules = render_nftables_ruleset(plan_for("replay_only"))
    assert "ip saddr 172.16.0.2 ip daddr 172.16.0.53 tcp dport 3128 accept" in replay_rules
    assert replay_rules.index("@always_blocked drop") < replay_rules.index("tcp dport 3128 accept")
    assert "limit rate over 50/second drop" in render_nftables_ruleset(plan_for("live"))


def test_faults_are_sampled_per_episode_within_range():
    fault_spec = NetworkFaultSpec(latency_ms=(0, 200), packet_loss=(0.0, 0.01))
    sampled = sample_faults(fault_spec, seed=4)
    assert sampled == sample_faults(fault_spec, seed=4)
    assert 0 <= sampled.latency_ms <= 200 and 0 <= sampled.packet_loss_percent <= 1.0
    assert sample_faults(NetworkFaultSpec(), seed=4) is None
    assert sample_faults(None, seed=4) is None
    plan = plan_for(faults=NetworkFaultSpec(latency_ms=(80, 80), packet_loss=(0.02, 0.02)))
    assert netem_command(plan) == ("tc", "qdisc", "replace", "dev", "tap0", "root", "netem", "delay", "80ms",
                                   "loss", "2.0%")
    assert netem_command(plan_for()) == ("tc", "qdisc", "del", "dev", "tap0", "root")


def test_certificates_are_valid_at_the_world_clock():
    authority = create_certificate_authority("Fake internet CA", WORLD_TIME)
    certificate_pem, key_pem = issue_server_certificate(authority, ["crm.example.com", "www.crm.example.com"],
                                                        WORLD_TIME)
    certificate = x509.load_pem_x509_certificate(certificate_pem)
    authority_certificate = x509.load_pem_x509_certificate(authority.certificate_pem)
    authority_certificate.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes,
                                              ec.ECDSA(certificate.signature_hash_algorithm))
    alternative_names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert alternative_names.get_values_for_type(x509.DNSName) == ["crm.example.com", "www.crm.example.com"]
    assert certificate.not_valid_before_utc <= WORLD_TIME <= certificate.not_valid_after_utc
    assert certificate.not_valid_after_utc < WORLD_TIME + timedelta(days=400)
    assert b"PRIVATE KEY" in key_pem
    with pytest.raises(ValueError):
        issue_server_certificate(authority, [], WORLD_TIME)


def test_artifacts_bundle_files_and_commands():
    authority = create_certificate_authority("Fake internet CA", WORLD_TIME)
    certificates = {"crm.example.com": issue_server_certificate(authority, ["crm.example.com"], WORLD_TIME)}
    artifacts = render_network_artifacts(plan_for("replay_only"), authority.certificate_pem, certificates)
    assert set(artifacts.files) == {"dnsmasq.conf", "nftables.conf", "ca.pem", "certs/crm.example.com.pem",
                                    "certs/crm.example.com.key"}
    assert artifacts.commands[0] == ("nft", "-f", "nftables.conf")
    assert all(isinstance(argument, str) for command in artifacts.commands for argument in command)
