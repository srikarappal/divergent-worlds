package nodeagent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"rlworld/fleet/internal/netplan"
	"rlworld/fleet/internal/snapshot"
	"rlworld/fleet/internal/vmm"
)

// windowRoles are the accessibility roles of top level windows on Linux (AT-SPI) and Windows (UIA).
var windowRoles = map[string]bool{"frame": true, "window": true, "dialog": true}

// BuildRequest boots an image once and snapshots it at app ready (DESIGN.md section 3, lifecycle step 1).
type BuildRequest struct {
	BuildID    string
	KernelPath string
	DiskPath   string
	MemoryMiB  int
	VCPUs      int
	// ReadyWindow is the window the image's readiness probe waits for; empty waits for the guest agent alone.
	ReadyWindow string
	// StableFor is how long the screen must hold still once the window is up.
	StableFor time.Duration
	// Timeout bounds the whole wait for readiness.
	Timeout   time.Duration
	OutputDir string
}

// BuildResult is where the base snapshot's files landed and how long the image took to become ready.
type BuildResult struct {
	StatePath  string
	MemoryPath string
	DiskPath   string
	VMMVersion string
	ReadyAfter time.Duration
}

// resolveSnapshot fills in the files of a snapshot named by digest: pulled first unless already on this host, or
// with its memory still arriving when the driver serves pages lazily. Leases that name files skip this, as do
// Docker leases, whose snapshot is the image named as their base disk.
func (agent *Agent) resolveSnapshot(ctx context.Context, request StartRequest) (StartRequest, *snapshot.LazyMemory,
	error) {
	if request.SnapshotDigest == "" || request.StatePath != "" || request.BaseDiskPath != "" {
		return request, nil, nil
	}
	cache := agent.config.Snapshots
	if cache == nil {
		return request, nil, fmt.Errorf("%w: lease names snapshot %s", ErrNoSnapshotStore, request.SnapshotDigest)
	}
	restorer, canRestoreLazily := agent.driver.(vmm.LazyRestorer)
	if agent.config.LazyMemory && canRestoreLazily && restorer.RestoresLazily() {
		opened, err := cache.OpenLazy(ctx, request.SnapshotDigest)
		if err != nil {
			return request, nil, fmt.Errorf("pull snapshot %s: %w", request.SnapshotDigest, err)
		}
		return withSnapshot(request, opened.Local), opened.Memory, nil
	}
	local, err := cache.Ensure(ctx, request.SnapshotDigest)
	if err != nil {
		return request, nil, fmt.Errorf("pull snapshot %s: %w", request.SnapshotDigest, err)
	}
	return withSnapshot(request, local), nil, nil
}

// withSnapshot points a request at a snapshot's files; a restore takes the memory and vCPUs it was taken with.
func withSnapshot(request StartRequest, local snapshot.Local) StartRequest {
	request.StatePath, request.MemoryPath, request.BaseDiskPath = local.StatePath, local.MemoryPath, local.DiskPath
	request.MemoryMiB, request.VCPUs = local.Manifest.MemoryMiB, local.Manifest.VCPUs
	return request
}

// Prefetch pulls a snapshot ahead of the leases a scheduled run will place on this host.
func (agent *Agent) Prefetch(ctx context.Context, digest string) error {
	if agent.config.Snapshots == nil {
		return ErrNoSnapshotStore
	}
	_, err := agent.config.Snapshots.Ensure(ctx, digest)
	return err
}

// CachedSnapshots lists the snapshots complete on this host.
func (agent *Agent) CachedSnapshots() ([]string, error) {
	if agent.config.Snapshots == nil {
		return nil, nil
	}
	return agent.config.Snapshots.Cached()
}

// BuildSnapshot boots an image's disk in a clone network slot with the devices every clone keeps, waits for the
// image's readiness probe, then writes the base snapshot: state, memory and the disk captured with that memory.
func (agent *Agent) BuildSnapshot(ctx context.Context, request BuildRequest) (BuildResult, error) {
	booter, canBoot := agent.driver.(vmm.Booter)
	if !canBoot || isolatesItself(agent.driver.Name()) {
		return BuildResult{}, fmt.Errorf("the %s driver cannot cold boot images", agent.driver.Name())
	}
	if err := os.MkdirAll(request.OutputDir, 0o755); err != nil {
		return BuildResult{}, err
	}
	result := BuildResult{StatePath: filepath.Join(request.OutputDir, snapshot.StateFile),
		MemoryPath: filepath.Join(request.OutputDir, snapshot.MemoryFile),
		DiskPath:   filepath.Join(request.OutputDir, snapshot.DiskFile)}
	index, err := agent.allocateIndex()
	if err != nil {
		return BuildResult{}, err
	}
	defer agent.freeIndex(index)
	network, err := netplan.PlanClone(index)
	if err != nil {
		return BuildResult{}, err
	}
	setup := append(network.SetupCommands(), []string{"cp", "--reflink=auto", request.DiskPath, result.DiskPath})
	if err := agent.runAll(ctx, setup); err != nil {
		agent.cleanup(ctx, network, "")
		return BuildResult{}, fmt.Errorf("prepare build %s: %w", request.BuildID, err)
	}
	defer agent.cleanup(ctx, network, "")
	if _, err := booter.Boot(ctx, vmm.BootSpec{VMID: request.BuildID, KernelPath: request.KernelPath,
		DiskPath: result.DiskPath, MemoryPath: result.MemoryPath, MemoryMiB: request.MemoryMiB, VCPUs: request.VCPUs,
		Namespace: network.Namespace}); err != nil {
		return BuildResult{}, fmt.Errorf("boot %s: %w", request.BuildID, err)
	}
	snapshotted, err := agent.snapshotWhenReady(ctx, request, result)
	return snapshotted, errors.Join(err, agent.driver.Destroy(ctx, request.BuildID))
}

// snapshotWhenReady waits for the readiness probe, reads the VMM version and takes the full snapshot.
func (agent *Agent) snapshotWhenReady(ctx context.Context, request BuildRequest,
	result BuildResult) (BuildResult, error) {
	readyAfter, err := agent.waitForReady(ctx, request)
	if err != nil {
		return BuildResult{}, err
	}
	result.ReadyAfter = readyAfter
	if versioner, reportsVersion := agent.driver.(vmm.Versioner); reportsVersion {
		if result.VMMVersion, err = versioner.VMMVersion(ctx, request.BuildID); err != nil {
			return BuildResult{}, err
		}
	}
	written, err := agent.driver.Snapshot(ctx, request.BuildID, vmm.Full, result.StatePath, result.MemoryPath)
	if err != nil {
		return BuildResult{}, fmt.Errorf("snapshot %s: %w", request.BuildID, err)
	}
	if written.MemoryPath != "" {
		result.MemoryPath = written.MemoryPath
	}
	return result, nil
}

// waitForReady polls the guest until the readiness probe holds: the agent answers, the probe window is open and
// the screen stays still for StableFor.
func (agent *Agent) waitForReady(ctx context.Context, request BuildRequest) (time.Duration, error) {
	started := agent.clock.Now()
	windowOpen := request.ReadyWindow == ""
	waitingFor := "the guest agent"
	lastHash, stableSince := "", started
	for {
		now := agent.clock.Now()
		if now.Sub(started) >= request.Timeout {
			return 0, fmt.Errorf("%s was not ready within %s: still waiting for %s", request.BuildID, request.Timeout,
				waitingFor)
		}
		if err := ctx.Err(); err != nil {
			return 0, err
		}
		if !windowOpen {
			observation, err := agent.guest.Observe(ctx, request.BuildID, []string{"accessibility_tree"})
			if err == nil {
				waitingFor = "the " + request.ReadyWindow + " window"
				windowOpen = hasWindow(observation, request.ReadyWindow)
			}
		} else if hash, err := agent.guest.ScreenHash(ctx, request.BuildID); err == nil {
			waitingFor = "the screen to hold still"
			if hash != lastHash {
				lastHash, stableSince = hash, now
			} else if now.Sub(stableSince) >= request.StableFor {
				return now.Sub(started), nil
			}
		}
		agent.sleep(agent.config.SettlePoll)
	}
}

// hasWindow reports whether an observation's accessibility nodes hold a top level window titled with the probe
// window's name, as "Untitled 1 - LibreOffice Calc" is for Calc.
func hasWindow(observation json.RawMessage, window string) bool {
	var decoded struct {
		Accessibility []struct {
			Role string `json:"role"`
			Name string `json:"name"`
		} `json:"accessibility"`
	}
	if err := json.Unmarshal(observation, &decoded); err != nil {
		return false
	}
	for _, node := range decoded.Accessibility {
		if windowRoles[node.Role] && strings.Contains(node.Name, window) {
			return true
		}
	}
	return false
}

// CaptureSnapshot writes a full snapshot of a restored env with a copy of the disk captured with that memory, as a
// world snapshot takes after its world is materialized. Only VMMs whose snapshots keep memory in its own file can
// be captured; a QEMU clone's private RAM goes into its state, so a new template would have to boot.
func (agent *Agent) CaptureSnapshot(ctx context.Context, envID, outputDir string) (BuildResult, error) {
	captured, err := agent.lookup(envID)
	if err != nil {
		return BuildResult{}, err
	}
	if isolatesItself(agent.driver.Name()) {
		return BuildResult{}, fmt.Errorf("the %s driver keeps no snapshot files to capture", agent.driver.Name())
	}
	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return BuildResult{}, err
	}
	result := BuildResult{StatePath: filepath.Join(outputDir, snapshot.StateFile),
		MemoryPath: filepath.Join(outputDir, snapshot.MemoryFile), DiskPath: filepath.Join(outputDir, snapshot.DiskFile)}
	written, err := agent.driver.Snapshot(ctx, envID, vmm.Full, result.StatePath, result.MemoryPath)
	if err != nil {
		return BuildResult{}, fmt.Errorf("snapshot %s: %w", envID, err)
	}
	agent.setState(envID, vmm.Paused)
	if written.MemoryPath == "" {
		return BuildResult{}, fmt.Errorf("the %s driver wrote %s's memory into its state", agent.driver.Name(), envID)
	}
	if err := agent.commands.Run(ctx, []string{"cp", "--reflink=auto", captured.overlayPath,
		result.DiskPath}); err != nil {
		return BuildResult{}, fmt.Errorf("copy the disk of %s: %w", envID, err)
	}
	if versioner, reportsVersion := agent.driver.(vmm.Versioner); reportsVersion {
		if result.VMMVersion, err = versioner.VMMVersion(ctx, envID); err != nil {
			return BuildResult{}, err
		}
	}
	return result, nil
}
