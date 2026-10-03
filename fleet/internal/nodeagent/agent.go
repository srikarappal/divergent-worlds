// Package nodeagent runs envs on one host (DESIGN.md sections 6.1 and 6.3).
//
// Starting an env wires its clone network, makes a copy on write disk overlay
// and restores the snapshot. Steps are turn based: the VM runs only while it
// acts and settles, then pauses while the model thinks, so slow and fast
// endpoints see the same world and paused VMs burn no CPU.
package nodeagent

import (
	"bufio"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"rlworld/fleet/internal/clock"
	"rlworld/fleet/internal/netplan"
	"rlworld/fleet/internal/vmm"
)

var (
	// ErrUnknownEnv means no env with that id runs on this host.
	ErrUnknownEnv = errors.New("unknown env")
	// ErrNoCloneSlots means every clone network slot is taken.
	ErrNoCloneSlots = errors.New("no free clone network slots on this host")
)

// GuestChannel reaches the guest agent (vsock on Linux, the in session helper on Windows); the VM must be running.
type GuestChannel interface {
	Prepare(ctx context.Context, vmID string, now time.Time, entropy []byte) error
	Act(ctx context.Context, vmID string, action json.RawMessage) error
	ScreenHash(ctx context.Context, vmID string) (string, error)
	Observe(ctx context.Context, vmID string, kinds []string) (json.RawMessage, error)
	Inject(ctx context.Context, vmID string, payload json.RawMessage) error
	Extract(ctx context.Context, vmID string, apps []string) (json.RawMessage, error)
	DialVNC(ctx context.Context, vmID string) (net.Conn, *bufio.Reader, error)
}

// entropyBytes is how much fresh randomness each restored clone's kernel RNG gets.
const entropyBytes = 64

// CommandRunner runs one argv on the host; tests record instead.
type CommandRunner interface {
	Run(ctx context.Context, argv []string) error
}

// Config holds host level settings.
type Config struct {
	HostID       string
	OverlayDir   string
	SettleStable time.Duration
	SettleCap    time.Duration
	SettlePoll   time.Duration
}

// StartRequest restores one env from a snapshot.
type StartRequest struct {
	EnvID        string
	StatePath    string
	MemoryPath   string
	BaseDiskPath string
	MemoryMiB    int
	VCPUs        int
	Resume       bool
	// WorldTime is the world's fixed clock; zero means the host clock.
	WorldTime time.Time
	// GuestElapsed is how long the guest has already run, for forks of a running env.
	GuestElapsed time.Duration
	// ObservationKinds are what the task observes: screenshot, accessibility_tree, dom, terminal, file_diff.
	ObservationKinds []string
}

// EnvInfo describes a running env.
type EnvInfo struct {
	EnvID        string
	State        vmm.State
	Namespace    string
	CloneAddress string
}

// StepResult is one turn: the observation plus where the time went.
type StepResult struct {
	Observation    json.RawMessage
	EnvDuration    time.Duration
	SettleDuration time.Duration
	SettleCapped   bool
}

type env struct {
	request      StartRequest
	network      netplan.CloneNetwork
	overlayPath  string
	state        vmm.State
	elapsed      time.Duration
	resumedAt    time.Time
	needsEntropy bool
}

// Agent manages every env on one host.
type Agent struct {
	mu            sync.Mutex
	config        Config
	driver        vmm.Driver
	guest         GuestChannel
	commands      CommandRunner
	clock         clock.Clock
	sleep         func(time.Duration)
	envs          map[string]*env
	usedIndexes   map[int]bool
	nextIndexHint int
}

// New returns an agent; nil sleep uses time.Sleep.
func New(config Config, driver vmm.Driver, guest GuestChannel, commands CommandRunner, agentClock clock.Clock,
	sleep func(time.Duration)) *Agent {
	if config.SettleStable <= 0 {
		config.SettleStable = 400 * time.Millisecond
	}
	if config.SettleCap <= 0 {
		config.SettleCap = 3 * time.Second
	}
	if config.SettlePoll <= 0 {
		config.SettlePoll = 100 * time.Millisecond
	}
	if sleep == nil {
		sleep = time.Sleep
	}
	return &Agent{config: config, driver: driver, guest: guest, commands: commands, clock: agentClock, sleep: sleep,
		envs: map[string]*env{}, usedIndexes: map[int]bool{}}
}

// isolatesItself is true for drivers whose VMs bring their own network and copy on write disk, like the Docker
// dev backend's containers: the node agent makes neither and forks start from the image a snapshot committed.
func isolatesItself(driverName string) bool {
	return driverName == "docker"
}

// cloneAddress is the clone's NAT address, or empty when the driver isolates its own VMs.
func cloneAddress(network netplan.CloneNetwork) string {
	if !network.CloneAddress.IsValid() {
		return ""
	}
	return network.CloneAddress.String()
}

// OverlayCommand makes a copy on write disk for one clone: a reflink copy of the raw image for
// Firecracker, a qcow2 overlay backed by the template disk for QEMU.
func OverlayCommand(driverName, baseDiskPath, overlayPath string) []string {
	if driverName == "qemu_templated" {
		return []string{"qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", baseDiskPath, overlayPath}
	}
	return []string{"cp", "--reflink=always", baseDiskPath, overlayPath}
}

func (agent *Agent) allocateIndex() (int, error) {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	for offset := range netplan.MaxClones {
		index := (agent.nextIndexHint + offset) % netplan.MaxClones
		if !agent.usedIndexes[index] {
			agent.usedIndexes[index] = true
			agent.nextIndexHint = index + 1
			return index, nil
		}
	}
	return 0, ErrNoCloneSlots
}

func (agent *Agent) freeIndex(index int) {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	delete(agent.usedIndexes, index)
}

func (agent *Agent) runAll(ctx context.Context, commands [][]string) error {
	for _, argv := range commands {
		if err := agent.commands.Run(ctx, argv); err != nil {
			return fmt.Errorf("%v: %w", argv, err)
		}
	}
	return nil
}

// Start wires the network, makes the overlay and restores; any failure undoes what was done.
func (agent *Agent) Start(ctx context.Context, request StartRequest) (EnvInfo, error) {
	index, err := agent.allocateIndex()
	if err != nil {
		return EnvInfo{}, err
	}
	network, err := netplan.PlanClone(index)
	if err != nil {
		agent.freeIndex(index)
		return EnvInfo{}, err
	}
	overlayPath := filepath.Join(agent.config.OverlayDir, request.EnvID+".disk")
	diskPath := overlayPath
	setup := append(network.SetupCommands(), OverlayCommand(agent.driver.Name(), request.BaseDiskPath, overlayPath))
	if isolatesItself(agent.driver.Name()) {
		network, overlayPath, diskPath, setup = netplan.CloneNetwork{Index: index}, "", request.BaseDiskPath, nil
	}
	if err := agent.runAll(ctx, setup); err != nil {
		agent.cleanup(ctx, network, overlayPath)
		agent.freeIndex(index)
		return EnvInfo{}, fmt.Errorf("prepare env %s: %w", request.EnvID, err)
	}
	restored, err := agent.driver.Restore(ctx, vmm.RestoreSpec{
		VMID: request.EnvID, StatePath: request.StatePath, MemoryPath: request.MemoryPath,
		DiskOverlayPath: diskPath, MemoryMiB: request.MemoryMiB, VCPUs: request.VCPUs,
		Namespace: network.Namespace, Resume: request.Resume,
	})
	if err != nil {
		agent.cleanup(ctx, network, overlayPath)
		agent.freeIndex(index)
		return EnvInfo{}, fmt.Errorf("restore env %s: %w", request.EnvID, err)
	}
	agent.mu.Lock()
	agent.envs[request.EnvID] = &env{request: request, network: network, overlayPath: overlayPath, state: restored.State,
		elapsed: request.GuestElapsed, needsEntropy: true}
	agent.mu.Unlock()
	return EnvInfo{EnvID: request.EnvID, State: restored.State, Namespace: network.Namespace,
		CloneAddress: cloneAddress(network)}, nil
}

// cleanup undoes what Start made; drivers that isolate their own VMs left nothing to undo.
func (agent *Agent) cleanup(ctx context.Context, network netplan.CloneNetwork, overlayPath string) {
	if network.Namespace != "" {
		for _, argv := range network.TeardownCommands() {
			agent.commands.Run(ctx, argv)
		}
	}
	if overlayPath != "" {
		agent.commands.Run(ctx, []string{"rm", "-f", overlayPath})
	}
}

func (agent *Agent) lookup(envID string) (*env, error) {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	found, ok := agent.envs[envID]
	if !ok {
		return nil, ErrUnknownEnv
	}
	return found, nil
}

// setState also keeps the guest's running time, which is all the time its world clock advances by.
func (agent *Agent) setState(envID string, state vmm.State) {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	found, ok := agent.envs[envID]
	if !ok {
		return
	}
	now := agent.clock.Now()
	if found.state == vmm.Running && state != vmm.Running && !found.resumedAt.IsZero() {
		found.elapsed += now.Sub(found.resumedAt)
		found.resumedAt = time.Time{}
	}
	if found.state != vmm.Running && state == vmm.Running {
		found.resumedAt = now
	}
	found.state = state
}

// guestClock is what the guest's clock should read: the world's time plus its running time, or the host clock.
func (agent *Agent) guestClock(found *env) time.Time {
	if found.request.WorldTime.IsZero() {
		return agent.clock.Now()
	}
	running := found.elapsed
	if !found.resumedAt.IsZero() {
		running += agent.clock.Now().Sub(found.resumedAt)
	}
	return found.request.WorldTime.Add(running)
}

// wake resumes the VM and prepares the guest: the clock every time, fresh entropy once after a restore.
func (agent *Agent) wake(ctx context.Context, envID string) error {
	if err := agent.Resume(ctx, envID); err != nil {
		return err
	}
	found, err := agent.lookup(envID)
	if err != nil {
		return err
	}
	agent.mu.Lock()
	now := agent.guestClock(found)
	needsEntropy := found.needsEntropy
	agent.mu.Unlock()
	var entropy []byte
	if needsEntropy {
		entropy = make([]byte, entropyBytes)
		if _, err := rand.Read(entropy); err != nil {
			return err
		}
	}
	if err := agent.guest.Prepare(ctx, envID, now, entropy); err != nil {
		return fmt.Errorf("prepare guest of %s: %w", envID, err)
	}
	agent.mu.Lock()
	found.needsEntropy = false
	agent.mu.Unlock()
	return nil
}

// Pause stops an env's vCPUs.
func (agent *Agent) Pause(ctx context.Context, envID string) error {
	if _, err := agent.lookup(envID); err != nil {
		return err
	}
	if err := agent.driver.Pause(ctx, envID); err != nil {
		return err
	}
	agent.setState(envID, vmm.Paused)
	return nil
}

// Resume restarts an env's vCPUs.
func (agent *Agent) Resume(ctx context.Context, envID string) error {
	if _, err := agent.lookup(envID); err != nil {
		return err
	}
	if err := agent.driver.Resume(ctx, envID); err != nil {
		return err
	}
	agent.setState(envID, vmm.Running)
	return nil
}

// Inject hands world state, task setup, sessions, license tokens and secrets to the guest, waking it briefly.
func (agent *Agent) Inject(ctx context.Context, envID string, payload json.RawMessage) error {
	if err := agent.wake(ctx, envID); err != nil {
		return errors.Join(err, agent.Pause(ctx, envID))
	}
	injectErr := agent.guest.Inject(ctx, envID, payload)
	return errors.Join(injectErr, agent.Pause(ctx, envID))
}

// Checkpoint pauses an env and writes a full snapshot of it.
func (agent *Agent) Checkpoint(ctx context.Context, envID, statePath, memoryPath string) (vmm.SnapshotRef, error) {
	if _, err := agent.lookup(envID); err != nil {
		return vmm.SnapshotRef{}, err
	}
	snapshot, err := agent.driver.Snapshot(ctx, envID, vmm.Full, statePath, memoryPath)
	if err == nil {
		agent.setState(envID, vmm.Paused)
	}
	return snapshot, err
}

// Extract reads normalized app state for verifiers; input is already frozen, so the VM wakes only to answer.
func (agent *Agent) Extract(ctx context.Context, envID string, apps []string) (json.RawMessage, error) {
	if err := agent.wake(ctx, envID); err != nil {
		return nil, errors.Join(err, agent.Pause(ctx, envID))
	}
	state, extractErr := agent.guest.Extract(ctx, envID, apps)
	return state, errors.Join(extractErr, agent.Pause(ctx, envID))
}

// DialVNC opens the env's screen for an operator; the caller keeps the env running while the session lasts.
func (agent *Agent) DialVNC(ctx context.Context, envID string) (net.Conn, *bufio.Reader, error) {
	if _, err := agent.lookup(envID); err != nil {
		return nil, nil, err
	}
	return agent.guest.DialVNC(ctx, envID)
}

// Destroy removes the VM, its network and its overlay, then frees the clone slot.
func (agent *Agent) Destroy(ctx context.Context, envID string) error {
	target, err := agent.lookup(envID)
	if err != nil {
		return err
	}
	destroyErr := agent.driver.Destroy(ctx, envID)
	agent.cleanup(ctx, target.network, target.overlayPath)
	agent.mu.Lock()
	delete(agent.envs, envID)
	delete(agent.usedIndexes, target.network.Index)
	agent.mu.Unlock()
	return destroyErr
}

// Fork checkpoints an env and starts count clones from the checkpoint; the source keeps its run state.
func (agent *Agent) Fork(ctx context.Context, envID string, count int, checkpointDir string) ([]EnvInfo, error) {
	source, err := agent.lookup(envID)
	if err != nil {
		return nil, err
	}
	wasRunning := source.state == vmm.Running
	checkpointPrefix := filepath.Join(checkpointDir, fmt.Sprintf("%s_at_%d", envID, agent.clock.Now().UnixNano()))
	checkpoint, err := agent.driver.Snapshot(ctx, envID, vmm.Full, checkpointPrefix+".state", checkpointPrefix+".memory")
	if err != nil {
		return nil, fmt.Errorf("checkpoint %s: %w", envID, err)
	}
	agent.setState(envID, vmm.Paused)
	checkpointDisk := checkpointPrefix + ".disk"
	if isolatesItself(agent.driver.Name()) {
		checkpointDisk = checkpoint.StatePath
	} else if err := agent.commands.Run(ctx, []string{"cp", "--reflink=always", source.overlayPath,
		checkpointDisk}); err != nil {
		return nil, fmt.Errorf("copy disk of %s: %w", envID, err)
	}
	var forks []EnvInfo
	for forkIndex := range count {
		forkRequest := source.request
		forkRequest.EnvID = fmt.Sprintf("%s_fork_%d", envID, forkIndex)
		forkRequest.StatePath, forkRequest.MemoryPath = checkpoint.StatePath, checkpoint.MemoryPath
		forkRequest.BaseDiskPath = checkpointDisk
		forkRequest.Resume = false
		forkRequest.GuestElapsed = agent.elapsedOf(source)
		info, err := agent.Start(ctx, forkRequest)
		if err != nil {
			for _, started := range forks {
				agent.Destroy(ctx, started.EnvID)
			}
			return nil, err
		}
		forks = append(forks, info)
	}
	if wasRunning {
		if err := agent.Resume(ctx, envID); err != nil {
			return forks, err
		}
	}
	return forks, nil
}

func (agent *Agent) elapsedOf(found *env) time.Duration {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	return found.elapsed
}

// Step runs one turn: resume, act, wait for the screen to settle, observe, pause.
func (agent *Agent) Step(ctx context.Context, envID string, action json.RawMessage) (StepResult, error) {
	return agent.turn(ctx, envID, action)
}

// Observe runs a turn without an action, for the first observation after a reset.
func (agent *Agent) Observe(ctx context.Context, envID string) (StepResult, error) {
	return agent.turn(ctx, envID, nil)
}

func (agent *Agent) turn(ctx context.Context, envID string, action json.RawMessage) (StepResult, error) {
	if _, err := agent.lookup(envID); err != nil {
		return StepResult{}, err
	}
	started := agent.clock.Now()
	if err := agent.wake(ctx, envID); err != nil {
		return StepResult{}, errors.Join(err, agent.Pause(ctx, envID))
	}
	result, stepErr := agent.actAndObserve(ctx, envID, action)
	pauseErr := agent.Pause(ctx, envID)
	result.EnvDuration = agent.clock.Now().Sub(started)
	return result, errors.Join(stepErr, pauseErr)
}

func (agent *Agent) actAndObserve(ctx context.Context, envID string, action json.RawMessage) (StepResult, error) {
	if action != nil {
		if err := agent.guest.Act(ctx, envID, action); err != nil {
			return StepResult{}, fmt.Errorf("act in %s: %w", envID, err)
		}
	}
	settleDuration, capped, err := agent.waitForSettle(ctx, envID)
	if err != nil {
		return StepResult{}, err
	}
	found, err := agent.lookup(envID)
	if err != nil {
		return StepResult{}, err
	}
	observation, err := agent.guest.Observe(ctx, envID, found.request.ObservationKinds)
	if err != nil {
		return StepResult{}, fmt.Errorf("observe %s: %w", envID, err)
	}
	return StepResult{Observation: observation, SettleDuration: settleDuration, SettleCapped: capped}, nil
}

// waitForSettle polls the screen hash until it holds for SettleStable, or gives up at SettleCap.
func (agent *Agent) waitForSettle(ctx context.Context, envID string) (time.Duration, bool, error) {
	started := agent.clock.Now()
	lastHash := ""
	stableSince := started
	for {
		hash, err := agent.guest.ScreenHash(ctx, envID)
		if err != nil {
			return 0, false, fmt.Errorf("screen hash of %s: %w", envID, err)
		}
		now := agent.clock.Now()
		if hash != lastHash {
			lastHash, stableSince = hash, now
		} else if now.Sub(stableSince) >= agent.config.SettleStable {
			return now.Sub(started), false, nil
		}
		if now.Sub(started) >= agent.config.SettleCap {
			return now.Sub(started), true, nil
		}
		if err := ctx.Err(); err != nil {
			return 0, false, err
		}
		agent.sleep(agent.config.SettlePoll)
	}
}

// Envs lists the host's envs in id order.
func (agent *Agent) Envs() []EnvInfo {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	var infos []EnvInfo
	for envID, current := range agent.envs {
		infos = append(infos, EnvInfo{EnvID: envID, State: current.state, Namespace: current.network.Namespace,
			CloneAddress: current.network.CloneAddress.String()})
	}
	slices.SortFunc(infos, compareEnvIDs)
	return infos
}

func compareEnvIDs(first, second EnvInfo) int {
	return strings.Compare(first.EnvID, second.EnvID)
}
