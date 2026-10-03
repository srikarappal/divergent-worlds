// Package fake is an in memory VMM driver for node agent tests.
package fake

import (
	"context"
	"fmt"
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
}

// New returns an empty fake driver named fake.
func New() *Driver {
	return &Driver{vms: map[string]vmm.VM{}, FailNext: map[string]error{}, DriverName: "fake"}
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
func (driver *Driver) Snapshot(_ context.Context, vmID string, kind vmm.SnapshotKind, statePath, memoryPath string) (vmm.SnapshotRef, error) {
	if err := driver.setState("snapshot", vmID, vmm.Paused); err != nil {
		return vmm.SnapshotRef{}, err
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
