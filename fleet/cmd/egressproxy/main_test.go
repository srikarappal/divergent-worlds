package main

import (
	"io"
	"net/netip"
	"slices"
	"strings"
	"testing"

	"rlworld/fleet/internal/egress"
)

func TestAllowlistModeTakesHostsPortsLimitsAndControlPlaneRanges(t *testing.T) {
	parsed, err := parseOptions([]string{"-env", "env_000042", "-allow", " docs.vendor.example, status.vendor.example ",
		"-ports", "443", "-rate", "2.5", "-burst", "5", "-max-tunnels", "8", "-block", "203.0.113.9/24",
		"-listen", "172.16.0.4:3128", "-log", "/var/log/rlworld/egress.jsonl"}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	policy := parsed.policy
	if parsed.env != "env_000042" || parsed.listen != "172.16.0.4:3128" ||
		parsed.logPath != "/var/log/rlworld/egress.jsonl" {
		t.Fatalf("options %+v", parsed)
	}
	if !slices.Equal(policy.Allowlist, []string{"docs.vendor.example", "status.vendor.example"}) || policy.AllowAll {
		t.Fatalf("allowlist %v", policy.Allowlist)
	}
	if !slices.Equal(policy.Ports, []int{443}) || policy.RequestsPerSecond != 2.5 || policy.Burst != 5 ||
		policy.MaxTunnels != 8 {
		t.Fatalf("limits %+v", policy)
	}
	if len(policy.Blocked) != len(egress.NonPublicPrefixes)+1 {
		t.Fatalf("blocked %v", policy.Blocked)
	}
	if policy.Blocked[len(policy.Blocked)-1] != netip.MustParsePrefix("203.0.113.0/24") {
		t.Fatalf("control plane range %v", policy.Blocked[len(policy.Blocked)-1])
	}
}

func TestLiveModeKeepsWebPortsAndDefaultLimits(t *testing.T) {
	parsed, err := parseOptions([]string{"-env", "env_1", "-allow-all"}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	if !parsed.policy.AllowAll || !slices.Equal(parsed.policy.Ports, []int{80, 443}) || parsed.listen != ":3128" {
		t.Fatalf("options %+v", parsed)
	}
	if parsed.policy.RequestsPerSecond != 10 || parsed.policy.MaxTunnels != 64 || parsed.logPath != "" {
		t.Fatalf("defaults %+v", parsed)
	}
	if !slices.Contains(parsed.policy.Blocked, netip.MustParsePrefix("169.254.0.0/16")) {
		t.Fatal("the metadata range is always blocked")
	}
}

func TestOptionsThatCannotServeAnEnvAreRefused(t *testing.T) {
	for _, testCase := range []struct {
		arguments []string
		message   string
	}{
		{[]string{"-allow-all"}, "-env"},
		{[]string{"-env", "e"}, "either -allow"},
		{[]string{"-env", "e", "-allow", "a.example", "-allow-all"}, "either -allow"},
		{[]string{"-env", "e", "-allow-all", "-ports", "80,http"}, "bad port"},
		{[]string{"-env", "e", "-allow-all", "-ports", "70000"}, "bad port"},
		{[]string{"-env", "e", "-allow-all", "-block", "not a range"}, "netip.ParsePrefix"},
		{[]string{"-env", "e", "-allow-all", "-rate", "-1"}, "may not be negative"},
		{[]string{"-env", "e", "-allow-all", "-unknown"}, "flag provided but not defined"},
	} {
		_, err := parseOptions(testCase.arguments, io.Discard)
		if err == nil || !strings.Contains(err.Error(), testCase.message) {
			t.Fatalf("%v: %v", testCase.arguments, err)
		}
	}
}
