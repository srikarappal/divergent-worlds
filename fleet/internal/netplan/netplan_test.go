package netplan

import (
	"slices"
	"strings"
	"testing"
)

func TestAddressesAreUniquePerClone(t *testing.T) {
	cases := []struct {
		index                         int
		host, namespace, cloneAddress string
	}{
		{0, "10.200.0.1", "10.200.0.2", "10.201.0.1"},
		{1, "10.200.0.5", "10.200.0.6", "10.201.0.2"},
		{63, "10.200.0.253", "10.200.0.254", "10.201.0.64"},
		{64, "10.200.1.1", "10.200.1.2", "10.201.0.65"},
		{16383, "10.200.255.253", "10.200.255.254", "10.201.64.0"},
	}
	for _, testCase := range cases {
		network, err := PlanClone(testCase.index)
		if err != nil {
			t.Fatalf("clone %d: %v", testCase.index, err)
		}
		got := []string{network.HostAddress.String(), network.NamespaceAddress.String(), network.CloneAddress.String()}
		want := []string{testCase.host, testCase.namespace, testCase.cloneAddress}
		if !slices.Equal(got, want) {
			t.Fatalf("clone %d addresses %v, want %v", testCase.index, got, want)
		}
		for _, interfaceName := range []string{network.HostVeth, network.NamespaceVeth, network.Namespace} {
			if len(interfaceName) > 15 {
				t.Fatalf("%q exceeds the 15 character interface name limit", interfaceName)
			}
		}
	}
	if _, err := PlanClone(MaxClones); err == nil {
		t.Fatal("indexes past the transit range must be rejected")
	}
}

func TestSetupKeepsTheSnapshotGuestAddressBehindNAT(t *testing.T) {
	network, _ := PlanClone(7)
	commands := []string{}
	for _, argv := range network.SetupCommands() {
		commands = append(commands, strings.Join(argv, " "))
	}
	joined := strings.Join(commands, "\n")
	for _, fragment := range []string{
		"ip netns add clone00007",
		"ip netns exec clone00007 ip tuntap add name tap0 mode tap",
		"-s 172.16.0.2 -j SNAT --to 10.201.0.8",
		"-d 10.201.0.8 -j DNAT --to 172.16.0.2",
		"ip route add 10.201.0.8 via 10.200.0.30",
	} {
		if !strings.Contains(joined, fragment) {
			t.Fatalf("setup commands lack %q:\n%s", fragment, joined)
		}
	}
	teardown := network.TeardownCommands()
	if strings.Join(teardown[len(teardown)-1], " ") != "ip netns del clone00007" {
		t.Fatalf("teardown %v", teardown)
	}
}
