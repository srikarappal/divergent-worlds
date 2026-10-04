// Package fake is an in memory VMM driver for node agent tests.
package fake

import (
	"context"
	"fmt"
	"os"
	"sync"

	"rlworld/fleet/internal/vmm"
)

// Driver keeps VMs in memory, logs every call and can be told to fail.
type Driver struct {
	mu         sync.Mutex
	vms        map[string]vmm.VM
	Operations []string
	FailNext   map[string]error
	// DriverName lets tests stand in for a named driver.
	DriverName string
	// Restores keeps every restore spec in order.
	Restores []vmm.RestoreSpec
	// Boots keeps every boot spec in order.
	Boots []vmm.BootSpec
	// Version is what VMMVersion reports.
	Version string
	// WritesSnapshots makes Snapshot write small state and memory files where a real VMM would.
	WritesSnapshots bool
}

// New returns an empty fake driver named fake.
func New() *Driver {
	return &Driver{vms: map[string]vmm.VM{}, FailNext: map[string]error{}, DriverName: "fake", Version: "fake-1"}
}

// Name identifies the driver.
func (driver *Driver) Name() string {
	return driver.DriverName
}

func (driver *Driver) record(operation, vmID string) error {
	driver.Operations = append(driver.Operations, fmt.Sprintf("%s %s", operation, vmID))
	if failure, found := driver.FailNext[operation]; found {
		delete(driver.FailNext, operation)
		return failure
	}
	return nil
}

// Restore creates a VM that is running or paused as requested.
func (driver *Driver) Restore(_ context.Context, spec vmm.RestoreSpec) (vmm.VM, error) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if err := driver.record("restore", spec.VMID); err != nil {
		return vmm.VM{}, err
	}
	driver.Restores = append(driver.Restores, spec)
	state := vmm.Paused
	if spec.Resume {
		state = vmm.Running
	}
	restored := vmm.VM{ID: spec.VMID, State: state, Driver: driver.Name()}
	driver.vms[spec.VMID] = restored
	return restored, nil
}

// Boot creates a running VM, as a cold boot does.
func (driver *Driver) Boot(_ context.Context, spec vmm.BootSpec) (vmm.VM, error) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if err := driver.record("boot", spec.VMID); err != nil {
		return vmm.VM{}, err
	}
	driver.Boots = append(driver.Boots, spec)
	booted := vmm.VM{ID: spec.VMID, State: vmm.Running, Driver: driver.Name()}
	driver.vms[spec.VMID] = booted
	return booted, nil
}

// VMMVersion reports Version for a VM that exists.
func (driver *Driver) VMMVersion(_ context.Context, vmID string) (string, error) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if _, found := driver.vms[vmID]; !found {
		return "", vmm.ErrUnknownVM
	}
	return driver.Version, driver.record("version", vmID)
}

func (driver *Driver) setState(operation, vmID string, state vmm.State) error {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	current, found := driver.vms[vmID]
	if !found {
		return vmm.ErrUnknownVM
	}
	if err := driver.record(operation, vmID); err != nil {
		return err
	}
	current.State = state
	driver.vms[vmID] = current
	return nil
}

// Pause stops the VM's vCPUs.
func (driver *Driver) Pause(_ context.Context, vmID string) error {
	return driver.setState("pause", vmID, vmm.Paused)
}

// Resume restarts the VM's vCPUs.
func (driver *Driver) Resume(_ context.Context, vmID string) error {
	return driver.setState("resume", vmID, vmm.Running)
}

// Snapshot pauses the VM and returns where the checkpoint would be.
func (driver *Driver) Snapshot(_ context.Context, vmID string, kind vmm.SnapshotKind, statePath,
	memoryPath string) (vmm.SnapshotRef, error) {
	if err := driver.setState("snapshot", vmID, vmm.Paused); err != nil {
		return vmm.SnapshotRef{}, err
	}
	if driver.WritesSnapshots {
		for path, content := range map[string]string{statePath: "state", memoryPath: "memory"} {
			if err := os.WriteFile(path, []byte(fmt.Sprintf("%s %s of %s", kind, content, vmID)), 0o644); err != nil {
				return vmm.SnapshotRef{}, err
			}
		}
	}
	return vmm.SnapshotRef{StatePath: statePath, MemoryPath: memoryPath, Kind: kind}, nil
}

// Destroy removes the VM.
func (driver *Driver) Destroy(_ context.Context, vmID string) error {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if _, found := driver.vms[vmID]; !found {
		return vmm.ErrUnknownVM
	}
	if err := driver.record("destroy", vmID); err != nil {
		return err
	}
	delete(driver.vms, vmID)
	return nil
}

// State reports a VM's state, or destroyed when it does not exist.
func (driver *Driver) State(vmID string) vmm.State {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if current, found := driver.vms[vmID]; found {
		return current.State
	}
	return vmm.Destroyed
}
