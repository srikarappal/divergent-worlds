// Package vmm defines the driver interface every VMM pool implements (DESIGN.md section 6.1).
//
// A driver restores a VM from a snapshot (memory plus disk overlay), pauses and
// resumes it, writes checkpoints and destroys it. Firecracker serves the Linux
// pool and QEMU templating serves the Windows pool; a fake serves tests.
package vmm

import (
	"context"
	"errors"
)

// State is a VM's run state.
type State string

const (
	// Running VMs execute guest code.
	Running State = "running"
	// Paused VMs hold memory but burn no CPU.
	Paused State = "paused"
	// Destroyed VMs are gone.
	Destroyed State = "destroyed"
)

// SnapshotKind says whether a checkpoint holds all memory or only pages dirtied since the last one.
type SnapshotKind string

const (
	// Full snapshots hold all guest memory.
	Full SnapshotKind = "full"
	// Diff snapshots hold dirtied pages only.
	Diff SnapshotKind = "diff"
)

// ErrUnknownVM means the driver has no VM with that id.
var ErrUnknownVM = errors.New("unknown vm")

// RestoreSpec describes one clone to restore.
type RestoreSpec struct {
	VMID            string
	StatePath       string
	MemoryPath      string
	DiskOverlayPath string
	MemoryMiB       int
	VCPUs           int
	Namespace       string
	Resume          bool
}

// SnapshotRef points at a written checkpoint.
type SnapshotRef struct {
	StatePath  string
	MemoryPath string
	Kind       SnapshotKind
}

// VM is a restored clone.
type VM struct {
	ID     string
	State  State
	Driver string
}

// Driver is what the node agent calls for every VM lifecycle step.
type Driver interface {
	Name() string
	Restore(ctx context.Context, spec RestoreSpec) (VM, error)
	Pause(ctx context.Context, vmID string) error
	Resume(ctx context.Context, vmID string) error
	Snapshot(ctx context.Context, vmID string, kind SnapshotKind, statePath, memoryPath string) (SnapshotRef, error)
	Destroy(ctx context.Context, vmID string) error
}

// FramebufferCapturer captures the screen at the hypervisor, out of the agent's reach.
type FramebufferCapturer interface {
	CaptureScreen(ctx context.Context, vmID, outputPath string) error
}

// InputInjector sends pointer and keyboard events at the hypervisor.
type InputInjector interface {
	SendPointer(ctx context.Context, vmID string, x, y, screenWidth, screenHeight int, button string, down bool) error
	SendKeys(ctx context.Context, vmID string, keys []string) error
}

// Process is a started VMM process.
type Process interface {
	PID() int
	Kill() error
	Wait() error
}

// Stop kills a VMM process and reaps it; Wait reports the kill itself, so only the kill's error counts.
func Stop(process Process) error {
	killErr := process.Kill()
	process.Wait()
	return killErr
}

// ProcessRunner starts VMM processes; tests swap in a fake.
type ProcessRunner interface {
	Start(ctx context.Context, argv []string) (Process, error)
}
