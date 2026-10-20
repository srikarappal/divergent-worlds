// Package docker runs development envs as containers behind the VMM driver interface (docs/TASKS.md build
// decision 4).
//
// It gives a laptop without KVM the same loop as the VM pools: restore starts a
// container from an image built by the dev image target, pause and resume use the
// cgroup freezer, snapshots commit the container's filesystem to an image and
// destroy removes the container. Containers boot instead of restoring memory, so
// forks keep files but restart processes; they share the laptop's kernel, so the
// world clock is not applied and they are for development only. Each container's
// guest agent listens on a loopback port and answers only its container's token.
package docker

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"rlworld/fleet/internal/vmm"
)

// Commander runs one docker CLI invocation and returns its standard output; tests record instead.
type Commander interface {
	Output(ctx context.Context, argv []string) ([]byte, error)
}

// ExecCommander runs the docker CLI.
type ExecCommander struct{}

// Output runs argv and folds the CLI's error text into the error.
func (ExecCommander) Output(ctx context.Context, argv []string) ([]byte, error) {
	output, err := exec.CommandContext(ctx, argv[0], argv[1:]...).Output()
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		command := strings.Join(argv[:min(2, len(argv))], " ")
		return nil, fmt.Errorf("%s: %w: %s", command, err, strings.TrimSpace(string(exitError.Stderr)))
	}
	return output, err
}

// Config tunes the containers; zero values take the defaults below.
type Config struct {
	// Binary is the docker CLI.
	Binary string
	// AgentPort is where the guest agent listens inside the container.
	AgentPort int
	// SecurityOptions default to seccomp=unconfined because Chrome's sandbox needs user namespaces, which
	// Docker's default seccomp profile blocks.
	SecurityOptions []string
	// ShmSizeMiB sizes /dev/shm; browsers crash with Docker's 64 MiB default.
	ShmSizeMiB int
	// ReadyTimeout bounds how long a booting container may take before its agent answers.
	ReadyTimeout time.Duration
	// SnapshotRepository names the images that snapshots commit.
	SnapshotRepository string
	// AllowEgress leaves a container's network open. Otherwise each container gets a firewall in its own network
	// namespace that lets only loopback traffic and replies to inbound connections, such as the agent's, out.
	AllowEgress bool
}

// egressRuleset is the nftables ruleset that closes a container's network. A docker exec with --privileged installs it,
// so the container itself never holds the capability to change it.
const egressRuleset = `table inet rlworld_egress {
	chain output {
		type filter hook output priority filter; policy accept;
		oif "lo" accept
		ct state established,related accept
		reject
	}
}`

type container struct {
	address string
	token   string
	state   vmm.State
}

// Driver runs one container per env.
type Driver struct {
	mu         sync.Mutex
	config     Config
	commander  Commander
	containers map[string]*container
	pollPeriod time.Duration
}

// NewDriver returns a driver with defaults filled in.
func NewDriver(config Config, commander Commander) *Driver {
	if config.Binary == "" {
		config.Binary = "docker"
	}
	if config.AgentPort == 0 {
		config.AgentPort = 5005
	}
	if config.SecurityOptions == nil {
		config.SecurityOptions = []string{"seccomp=unconfined"}
	}
	if config.ShmSizeMiB == 0 {
		config.ShmSizeMiB = 1024
	}
	if config.ReadyTimeout <= 0 {
		config.ReadyTimeout = 2 * time.Minute
	}
	if config.SnapshotRepository == "" {
		config.SnapshotRepository = "rlworld-snapshot"
	}
	return &Driver{config: config, commander: commander, containers: map[string]*container{},
		pollPeriod: 250 * time.Millisecond}
}

// Name identifies the driver.
func (driver *Driver) Name() string {
	return "docker"
}

// ContainerName is the container that runs one env.
func ContainerName(vmID string) string {
	return "rlworld-" + vmID
}

func newToken() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

func (driver *Driver) docker(ctx context.Context, arguments ...string) ([]byte, error) {
	return driver.commander.Output(ctx, append([]string{driver.config.Binary}, arguments...))
}

// RunArgs builds the docker run command; the image is the disk the node agent hands over.
func (driver *Driver) RunArgs(spec vmm.RestoreSpec, token string) []string {
	argv := []string{driver.config.Binary, "run", "--detach", "--init", "--name", ContainerName(spec.VMID),
		"--label", "rlworld.env=" + spec.VMID,
		"--publish", fmt.Sprintf("127.0.0.1::%d", driver.config.AgentPort),
		"--env", "RLWORLD_AGENT_TOKEN=" + token,
		"--shm-size", fmt.Sprintf("%dm", driver.config.ShmSizeMiB)}
	for _, option := range driver.config.SecurityOptions {
		argv = append(argv, "--security-opt", option)
	}
	if spec.VCPUs > 0 {
		argv = append(argv, "--cpus", strconv.Itoa(spec.VCPUs))
	}
	if spec.MemoryMiB > 0 {
		argv = append(argv, "--memory", fmt.Sprintf("%dm", spec.MemoryMiB))
	}
	return append(argv, spec.DiskOverlayPath)
}

// Restore starts the container, waits for its agent and pauses it unless asked to resume.
func (driver *Driver) Restore(ctx context.Context, spec vmm.RestoreSpec) (vmm.VM, error) {
	if spec.DiskOverlayPath == "" {
		return vmm.VM{}, fmt.Errorf("env %s names no image", spec.VMID)
	}
	token, err := newToken()
	if err != nil {
		return vmm.VM{}, err
	}
	name := ContainerName(spec.VMID)
	driver.docker(ctx, "rm", "--force", name)
	if _, err := driver.commander.Output(ctx, driver.RunArgs(spec, token)); err != nil {
		return vmm.VM{}, fmt.Errorf("start container for %s: %w", spec.VMID, err)
	}
	address, err := driver.publishedAddress(ctx, name)
	if err == nil && !driver.config.AllowEgress {
		err = driver.blockEgress(ctx, name)
	}
	if err == nil {
		err = driver.waitForAgent(ctx, address, token)
	}
	state := vmm.Running
	if err == nil && !spec.Resume {
		_, err = driver.docker(ctx, "pause", name)
		state = vmm.Paused
	}
	if err != nil {
		driver.docker(context.Background(), "rm", "--force", name)
		return vmm.VM{}, fmt.Errorf("boot container for %s: %w", spec.VMID, err)
	}
	driver.mu.Lock()
	driver.containers[spec.VMID] = &container{address: address, token: token, state: state}
	driver.mu.Unlock()
	return vmm.VM{ID: spec.VMID, State: state, Driver: driver.Name()}, nil
}

// blockEgress installs the egress firewall in the container's network namespace before the guest is used.
func (driver *Driver) blockEgress(ctx context.Context, name string) error {
	_, err := driver.docker(ctx, "exec", "--privileged", "--user", "0", name, "/usr/sbin/nft", egressRuleset)
	if err != nil {
		return fmt.Errorf("block egress: %w", err)
	}
	return nil
}

func (driver *Driver) publishedAddress(ctx context.Context, name string) (string, error) {
	output, err := driver.docker(ctx, "port", name, fmt.Sprintf("%d/tcp", driver.config.AgentPort))
	if err != nil {
		return "", err
	}
	address, _, _ := strings.Cut(strings.TrimSpace(string(output)), "\n")
	if address == "" {
		return "", fmt.Errorf("container %s publishes no agent port", name)
	}
	return address, nil
}

// agentAnswers is true once the agent accepts the token, which is when the container has finished booting.
func agentAnswers(address, token string, deadline time.Time) bool {
	connection, err := net.DialTimeout("tcp", address, time.Until(deadline))
	if err != nil {
		return false
	}
	defer connection.Close()
	if connection.SetDeadline(deadline) != nil {
		return false
	}
	if _, err := fmt.Fprintf(connection, "AUTH %s\n", token); err != nil {
		return false
	}
	acknowledgement, err := bufio.NewReader(connection).ReadString('\n')
	return err == nil && strings.HasPrefix(acknowledgement, "OK")
}

func (driver *Driver) waitForAgent(ctx context.Context, address, token string) error {
	deadline := time.Now().Add(driver.config.ReadyTimeout)
	for {
		attemptDeadline := time.Now().Add(2 * time.Second)
		if attemptDeadline.After(deadline) {
			attemptDeadline = deadline
		}
		if agentAnswers(address, token, attemptDeadline) {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("guest agent at %s did not answer within %s", address, driver.config.ReadyTimeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(driver.pollPeriod):
		}
	}
}

func (driver *Driver) lookup(vmID string) (*container, error) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	found, ok := driver.containers[vmID]
	if !ok {
		return nil, fmt.Errorf("%w: %s", vmm.ErrUnknownVM, vmID)
	}
	return found, nil
}

func (driver *Driver) setState(vmID string, state vmm.State) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if found, ok := driver.containers[vmID]; ok {
		found.state = state
	}
}

// Pause freezes every process in the container.
func (driver *Driver) Pause(ctx context.Context, vmID string) error {
	found, err := driver.lookup(vmID)
	if err != nil {
		return err
	}
	if found.state == vmm.Paused {
		return nil
	}
	if _, err := driver.docker(ctx, "pause", ContainerName(vmID)); err != nil {
		return err
	}
	driver.setState(vmID, vmm.Paused)
	return nil
}

// Resume thaws the container.
func (driver *Driver) Resume(ctx context.Context, vmID string) error {
	found, err := driver.lookup(vmID)
	if err != nil {
		return err
	}
	if found.state == vmm.Running {
		return nil
	}
	if _, err := driver.docker(ctx, "unpause", ContainerName(vmID)); err != nil {
		return err
	}
	driver.setState(vmID, vmm.Running)
	return nil
}

// SnapshotTag names the image a snapshot commits, after the state path the node agent chose.
func (driver *Driver) SnapshotTag(vmID, statePath string) string {
	tag := strings.Map(tagRune, strings.TrimSuffix(filepath.Base(statePath), filepath.Ext(statePath)))
	if strings.Trim(tag, ".") == "" {
		tag = vmID
	}
	return fmt.Sprintf("%s:%s", driver.config.SnapshotRepository, tag[:min(len(tag), 128)])
}

func tagRune(character rune) rune {
	if character >= 'a' && character <= 'z' || character >= 'A' && character <= 'Z' ||
		character >= '0' && character <= '9' || character == '_' || character == '.' || character == '-' {
		return character
	}
	return '_'
}

// Snapshot pauses the container and commits its filesystem; memory is not kept, so restores boot again.
func (driver *Driver) Snapshot(ctx context.Context, vmID string, kind vmm.SnapshotKind, statePath, memoryPath string) (
	vmm.SnapshotRef, error) {
	if err := driver.Pause(ctx, vmID); err != nil {
		return vmm.SnapshotRef{}, err
	}
	tag := driver.SnapshotTag(vmID, statePath)
	if _, err := driver.docker(ctx, "commit", "--pause=false", ContainerName(vmID), tag); err != nil {
		return vmm.SnapshotRef{}, fmt.Errorf("commit %s: %w", vmID, err)
	}
	return vmm.SnapshotRef{StatePath: tag, Kind: vmm.Full}, nil
}

// Destroy removes the container; committed snapshot images stay for the envs forked from them.
func (driver *Driver) Destroy(ctx context.Context, vmID string) error {
	if _, err := driver.lookup(vmID); err != nil {
		return err
	}
	_, err := driver.docker(ctx, "rm", "--force", ContainerName(vmID))
	driver.mu.Lock()
	delete(driver.containers, vmID)
	driver.mu.Unlock()
	return err
}

// GuestEndpoint is where the container's agent listens and the token it expects.
func (driver *Driver) GuestEndpoint(vmID string) (string, string, error) {
	found, err := driver.lookup(vmID)
	if err != nil {
		return "", "", err
	}
	return found.address, found.token, nil
}
