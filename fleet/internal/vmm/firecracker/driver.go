package firecracker

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"rlworld/fleet/internal/vmm"
)

// Config locates the binaries and the jail layout.
type Config struct {
	JailerPath        string
	FirecrackerPath   string
	ChrootBaseDir     string
	UID               int
	GID               int
	UseUffd           bool
	SocketWaitTimeout time.Duration
}

// APIFactory builds the API client for a socket; tests can wrap it.
type APIFactory func(socketPath string) API

type instance struct {
	process vmm.Process
	api     API
	state   vmm.State
}

// Driver runs each VM as a jailed Firecracker process in the clone's network namespace.
type Driver struct {
	mu         sync.Mutex
	config     Config
	runner     vmm.ProcessRunner
	newAPI     APIFactory
	vmsByID    map[string]*instance
	pollPeriod time.Duration
}

// NewDriver returns a driver; a nil factory uses the real API client.
func NewDriver(config Config, runner vmm.ProcessRunner, newAPI APIFactory) *Driver {
	if newAPI == nil {
		newAPI = NewAPI
	}
	if config.SocketWaitTimeout <= 0 {
		config.SocketWaitTimeout = 10 * time.Second
	}
	return &Driver{config: config, runner: runner, newAPI: newAPI, vmsByID: map[string]*instance{},
		pollPeriod: 5 * time.Millisecond}
}

// Name identifies the driver.
func (driver *Driver) Name() string {
	return "firecracker"
}

// JailRoot is where the jailer chroots one VM.
func (driver *Driver) JailRoot(vmID string) string {
	return filepath.Join(driver.config.ChrootBaseDir, filepath.Base(driver.config.FirecrackerPath), vmID, "root")
}

// SocketPath is the API socket as seen from the host.
func (driver *Driver) SocketPath(vmID string) string {
	return filepath.Join(driver.JailRoot(vmID), "run", "firecracker.socket")
}

// VsockPath is the host side socket of the VM's vsock device, which the snapshot names v.sock inside the jail.
func (driver *Driver) VsockPath(vmID string) string {
	return filepath.Join(driver.JailRoot(vmID), "v.sock")
}

// JailerArgs builds the jailer command line; snapshot paths are inside the jail root.
func (driver *Driver) JailerArgs(spec vmm.RestoreSpec) []string {
	argv := []string{
		driver.config.JailerPath,
		"--id", spec.VMID,
		"--exec-file", driver.config.FirecrackerPath,
		"--uid", strconv.Itoa(driver.config.UID),
		"--gid", strconv.Itoa(driver.config.GID),
		"--chroot-base-dir", driver.config.ChrootBaseDir,
		"--cgroup-version", "2",
	}
	if spec.VCPUs > 0 {
		argv = append(argv, "--cgroup", fmt.Sprintf("cpu.max=%d 100000", spec.VCPUs*100000))
	}
	if spec.MemoryMiB > 0 {
		argv = append(argv, "--cgroup", fmt.Sprintf("memory.max=%d", int64(spec.MemoryMiB+256)*1024*1024))
	}
	if spec.Namespace != "" {
		argv = append(argv, "--netns", filepath.Join("/var/run/netns", spec.Namespace))
	}
	return append(argv, "--", "--api-sock", "/run/firecracker.socket")
}

func (driver *Driver) waitForSocket(ctx context.Context, socketPath string) error {
	deadline := time.Now().Add(driver.config.SocketWaitTimeout)
	for {
		if _, err := os.Stat(socketPath); err == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("api socket %s did not appear within %s", socketPath, driver.config.SocketWaitTimeout)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(driver.pollPeriod):
		}
	}
}

// Restore starts the jailed process and loads the snapshot into it.
func (driver *Driver) Restore(ctx context.Context, spec vmm.RestoreSpec) (vmm.VM, error) {
	process, err := driver.runner.Start(ctx, driver.JailerArgs(spec))
	if err != nil {
		return vmm.VM{}, fmt.Errorf("start jailer for %s: %w", spec.VMID, err)
	}
	socketPath := driver.SocketPath(spec.VMID)
	if err := driver.waitForSocket(ctx, socketPath); err != nil {
		_ = process.Kill()
		return vmm.VM{}, err
	}
	api := driver.newAPI(socketPath)
	if err := api.LoadSnapshot(ctx, spec.StatePath, spec.MemoryPath, driver.config.UseUffd, spec.Resume); err != nil {
		_ = process.Kill()
		return vmm.VM{}, fmt.Errorf("load snapshot into %s: %w", spec.VMID, err)
	}
	state := vmm.Paused
	if spec.Resume {
		state = vmm.Running
	}
	driver.mu.Lock()
	driver.vmsByID[spec.VMID] = &instance{process: process, api: api, state: state}
	driver.mu.Unlock()
	return vmm.VM{ID: spec.VMID, State: state, Driver: driver.Name()}, nil
}

func (driver *Driver) lookup(vmID string) (*instance, error) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	found, ok := driver.vmsByID[vmID]
	if !ok {
		return nil, vmm.ErrUnknownVM
	}
	return found, nil
}

func (driver *Driver) setPaused(ctx context.Context, vmID string, paused bool) error {
	target, err := driver.lookup(vmID)
	if err != nil {
		return err
	}
	if err := target.api.SetPaused(ctx, paused); err != nil {
		return err
	}
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if paused {
		target.state = vmm.Paused
	} else {
		target.state = vmm.Running
	}
	return nil
}

// Pause stops the vCPUs.
func (driver *Driver) Pause(ctx context.Context, vmID string) error {
	return driver.setPaused(ctx, vmID, true)
}

// Resume restarts the vCPUs.
func (driver *Driver) Resume(ctx context.Context, vmID string) error {
	return driver.setPaused(ctx, vmID, false)
}

// Snapshot pauses the VM (Firecracker requires it) and writes a Full or Diff snapshot.
func (driver *Driver) Snapshot(ctx context.Context, vmID string, kind vmm.SnapshotKind, statePath, memoryPath string) (vmm.SnapshotRef, error) {
	if err := driver.Pause(ctx, vmID); err != nil {
		return vmm.SnapshotRef{}, err
	}
	target, err := driver.lookup(vmID)
	if err != nil {
		return vmm.SnapshotRef{}, err
	}
	snapshotType := "Full"
	if kind == vmm.Diff {
		snapshotType = "Diff"
	}
	if err := target.api.CreateSnapshot(ctx, snapshotType, statePath, memoryPath); err != nil {
		return vmm.SnapshotRef{}, err
	}
	return vmm.SnapshotRef{StatePath: statePath, MemoryPath: memoryPath, Kind: kind}, nil
}

// Destroy kills the process and removes the jail.
func (driver *Driver) Destroy(_ context.Context, vmID string) error {
	target, err := driver.lookup(vmID)
	if err != nil {
		return err
	}
	killErr := target.process.Kill()
	removeErr := os.RemoveAll(filepath.Dir(driver.JailRoot(vmID)))
	driver.mu.Lock()
	delete(driver.vmsByID, vmID)
	driver.mu.Unlock()
	return errors.Join(killErr, removeErr)
}
