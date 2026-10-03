// Package netplan wires one clone's network namespace (DESIGN.md section 6.1).
//
// Every clone keeps the guest address it had when the snapshot was taken, so
// each runs in its own namespace with a tap of the same name. A veth pair
// reaches the host, and NAT gives every clone a unique address outside its
// namespace, following Firecracker's network for clones layout.
package netplan

import (
	"fmt"
	"net/netip"
)

const (
	// MaxClones is how many /30 links fit in the 10.200.0.0/16 transit range.
	MaxClones = 16384
	// GuestAddress is the address every clone's guest was snapshotted with.
	GuestAddress = "172.16.0.2"
	// TapGatewayCIDR is the tap side of the guest link inside each namespace.
	TapGatewayCIDR = "172.16.0.1/24"
	// TapName is the tap every snapshot expects.
	TapName = "tap0"
)

var (
	transitBase = netip.MustParseAddr("10.200.0.0")
	cloneBase   = netip.MustParseAddr("10.201.0.0")
)

// CloneNetwork names and addresses for one clone.
type CloneNetwork struct {
	Index            int
	Namespace        string
	HostVeth         string
	NamespaceVeth    string
	HostAddress      netip.Addr
	NamespaceAddress netip.Addr
	CloneAddress     netip.Addr
}

func offset(base netip.Addr, amount int) netip.Addr {
	address := base.As4()
	value := uint32(address[0])<<24 | uint32(address[1])<<16 | uint32(address[2])<<8 | uint32(address[3])
	value += uint32(amount)
	return netip.AddrFrom4([4]byte{byte(value >> 24), byte(value >> 16), byte(value >> 8), byte(value)})
}

// PlanClone returns the network layout for clone number index.
func PlanClone(index int) (CloneNetwork, error) {
	if index < 0 || index >= MaxClones {
		return CloneNetwork{}, fmt.Errorf("clone index %d is outside 0 to %d", index, MaxClones-1)
	}
	return CloneNetwork{
		Index:            index,
		Namespace:        fmt.Sprintf("clone%05d", index),
		HostVeth:         fmt.Sprintf("vh%05d", index),
		NamespaceVeth:    fmt.Sprintf("vn%05d", index),
		HostAddress:      offset(transitBase, index*4+1),
		NamespaceAddress: offset(transitBase, index*4+2),
		CloneAddress:     offset(cloneBase, index+1),
	}, nil
}

func inNamespace(namespace string, argv ...string) []string {
	return append([]string{"ip", "netns", "exec", namespace}, argv...)
}

// SetupCommands creates the namespace, the tap, the veth pair, routes and NAT, as argv lists.
func (network CloneNetwork) SetupCommands() [][]string {
	return [][]string{
		{"ip", "netns", "add", network.Namespace},
		inNamespace(network.Namespace, "ip", "tuntap", "add", "name", TapName, "mode", "tap"),
		inNamespace(network.Namespace, "ip", "addr", "add", TapGatewayCIDR, "dev", TapName),
		inNamespace(network.Namespace, "ip", "link", "set", TapName, "up"),
		{"ip", "link", "add", network.HostVeth, "type", "veth", "peer", "name", network.NamespaceVeth},
		{"ip", "link", "set", network.NamespaceVeth, "netns", network.Namespace},
		{"ip", "addr", "add", network.HostAddress.String() + "/30", "dev", network.HostVeth},
		{"ip", "link", "set", network.HostVeth, "up"},
		inNamespace(network.Namespace, "ip", "addr", "add", network.NamespaceAddress.String()+"/30", "dev",
			network.NamespaceVeth),
		inNamespace(network.Namespace, "ip", "link", "set", network.NamespaceVeth, "up"),
		inNamespace(network.Namespace, "ip", "link", "set", "lo", "up"),
		inNamespace(network.Namespace, "ip", "route", "add", "default", "via", network.HostAddress.String()),
		inNamespace(network.Namespace, "iptables", "-t", "nat", "-A", "POSTROUTING", "-o", network.NamespaceVeth,
			"-s", GuestAddress, "-j", "SNAT", "--to", network.CloneAddress.String()),
		inNamespace(network.Namespace, "iptables", "-t", "nat", "-A", "PREROUTING", "-i", network.NamespaceVeth,
			"-d", network.CloneAddress.String(), "-j", "DNAT", "--to", GuestAddress),
		{"ip", "route", "add", network.CloneAddress.String(), "via", network.NamespaceAddress.String()},
	}
}

// TeardownCommands remove the route and the namespace; deleting the namespace removes the veth pair too.
func (network CloneNetwork) TeardownCommands() [][]string {
	return [][]string{
		{"ip", "route", "del", network.CloneAddress.String()},
		{"ip", "netns", "del", network.Namespace},
	}
}
