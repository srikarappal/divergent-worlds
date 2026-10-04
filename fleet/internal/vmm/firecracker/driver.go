package firecracker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"rlworld/fleet/internal/netplan"
	"rlworld/fleet/internal/vmm"
)

// The names a snapshot records for its files inside the jail. The builder boots with them and every restore links
// the clone's own files in under the same names, so the paths Firecracker saved in the snapshot resolve.
const (
	jailKernel           = "vmlinux"
	jailDisk             = "rootfs.ext4"
	jailVsock            = "v.sock"
	jailState            = "snapshot.state"
	jailMemory           = "snapshot.memory"
	jailPageSocket       = "uffd.sock"
	jailCheckpointState  = "checkpoint.state"
	jailCheckpointMemory = "checkpoint.memory"
	guestCID             = 3
	// DefaultBootArgs boot the image's init on the serial console; Firecracker adds the root device itself.
	DefaultBootArgs = "console=ttyS0 reboot=k panic=1 pci=off"
)

// Config locates the binaries and the jail layout. The jail root, the clone overlays and the snapshot cache share
// one filesystem: files enter a jail as hard links.
type Config struct {
	JailerPath      string
	FirecrackerPath string
	ChrootBaseDir   string
	UID             int
	GID             int
	// CPUTemplatePath is the custom CPU template every VM of the pool boots with, so snapshots move between its
	// hosts (DESIGN.md 6.1.6).
	CPUTemplatePath string
	// BootArgs default to DefaultBootArgs.
	BootArgs          string
	SocketWaitTimeout time.Duration
}

// APIFactory builds the API client for a socket; tests can wrap it.
type APIFactory func(socketPath string) API

type instance struct {
	process vmm.Process
	api     API
	state   vmm.State
	pages   *pageServer
}

// jailFile is a host file linked into a jail under the name the snapshot expects.
type jailFile struct {
	name     string
	hostPath string
	writable bool
}

// Driver runs each VM as a jailed Firecracker process in the clone's network namespace.
type Driver struct {
	mu         sync.Mutex
	config     Config
	runner     vmm.ProcessRunner
	newAPI     APIFactory
	chown      func(name string, uid, gid int) error
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
	if config.BootArgs == "" {
		config.BootArgs = DefaultBootArgs
	}
	return &Driver{config: config, runner: runner, newAPI: newAPI, chown: os.Chown, vmsByID: map[string]*instance{},
		pollPeriod: 5 * time.Millisecond}
}

// Name identifies the driver.
func (driver *Driver) Name() string {
	return "firecracker"
}

// RestoresLazily is true: a page server answers faults while the memory file is still being pulled.
func (driver *Driver) RestoresLazily() bool {
	return true
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
	return filepath.Join(driver.JailRoot(vmID), jailVsock)
}

// JailerArgs builds the jailer command line for one VM.
func (driver *Driver) JailerArgs(vmID string, vcpus, memoryMiB int, namespace string) []string {
	argv := []string{
		driver.config.JailerPath,
		"--id", vmID,
		"--exec-file", driver.config.FirecrackerPath,
		"--uid", strconv.Itoa(driver.config.UID),
		"--gid", strconv.Itoa(driver.config.GID),
		"--chroot-base-dir", driver.config.ChrootBaseDir,
		"--cgroup-version", "2",
	}
	if vcpus > 0 {
		argv = append(argv, "--cgroup", fmt.Sprintf("cpu.max=%d 100000", vcpus*100000))
	}
	if memoryMiB > 0 {
		argv = append(argv, "--cgroup", fmt.Sprintf("memory.max=%d", int64(memoryMiB+256)*1024*1024))
	}
	if namespace != "" {
		argv = append(argv, "--netns", filepath.Join("/var/run/netns", namespace))
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

// startJailed starts the jailer and waits until Firecracker answers on its API socket.
func (driver *Driver) startJailed(ctx context.Context, vmID string, vcpus, memoryMiB int,
	namespace string) (vmm.Process, API, error) {
	process, err := driver.runner.Start(ctx, driver.JailerArgs(vmID, vcpus, memoryMiB, namespace))
	if err != nil {
		return nil, nil, fmt.Errorf("start jailer for %s: %w", vmID, err)
	}
	socketPath := driver.SocketPath(vmID)
	if err := driver.waitForSocket(ctx, socketPath); err != nil {
		driver.abandon(vmID, process)
		return nil, nil, err
	}
	return process, driver.newAPI(socketPath), nil
}

// abandon stops a VM that never became usable and removes its jail.
func (driver *Driver) abandon(vmID string, process vmm.Process) {
	vmm.Stop(process)
	os.RemoveAll(filepath.Dir(driver.JailRoot(vmID)))
}

// linkIntoJail hard links host files into a jail; the VM's user gets the files it writes.
func (driver *Driver) linkIntoJail(vmID string, files []jailFile) error {
	root := driver.JailRoot(vmID)
	for _, file := range files {
		target := filepath.Join(root, file.name)
		if err := os.Link(file.hostPath, target); err != nil {
			return fmt.Errorf("link %s into the jail of %s (jails, overlays and the snapshot cache share a "+
				"filesystem): %w", file.hostPath, vmID, err)
		}
		if file.writable {
			if err := driver.chown(target, driver.config.UID, driver.config.GID); err != nil {
				return err
			}
		}
	}
	return nil
}

func (driver *Driver) cpuTemplate() (json.RawMessage, error) {
	if driver.config.CPUTemplatePath == "" {
		return nil, nil
	}
	raw, err := os.ReadFile(driver.config.CPUTemplatePath)
	if err != nil {
		return nil, err
	}
	if !json.Valid(raw) {
		return nil, fmt.Errorf("cpu template %s is not JSON", driver.config.CPUTemplatePath)
	}
	return raw, nil
}

func (driver *Driver) track(vmID string, tracked *instance) vmm.VM {
	driver.mu.Lock()
	driver.vmsByID[vmID] = tracked
	driver.mu.Unlock()
	return vmm.VM{ID: vmID, State: tracked.state, Driver: driver.Name()}
}

// Boot cold boots an image's kernel and root disk with the vsock device and NIC every clone of its snapshot keeps.
func (driver *Driver) Boot(ctx context.Context, spec vmm.BootSpec) (vmm.VM, error) {
	template, err := driver.cpuTemplate()
	if err != nil {
		return vmm.VM{}, err
	}
	process, api, err := driver.startJailed(ctx, spec.VMID, spec.VCPUs, spec.MemoryMiB, spec.Namespace)
	if err != nil {
		return vmm.VM{}, err
	}
	files := []jailFile{{name: jailKernel, hostPath: spec.KernelPath},
		{name: jailDisk, hostPath: spec.DiskPath, writable: true}}
	if err := driver.linkIntoJail(spec.VMID, files); err != nil {
		driver.abandon(spec.VMID, process)
		return vmm.VM{}, err
	}
	config := BootConfig{VCPUs: spec.VCPUs, MemoryMiB: spec.MemoryMiB, CPUTemplate: template, KernelPath: jailKernel,
		BootArgs: driver.config.BootArgs, DiskPath: jailDisk, VsockPath: jailVsock, GuestCID: guestCID,
		TapName: netplan.TapName, GuestMAC: netplan.GuestMAC}
	if err := api.BootVM(ctx, config); err != nil {
		driver.abandon(spec.VMID, process)
		return vmm.VM{}, fmt.Errorf("boot %s: %w", spec.VMID, err)
	}
	return driver.track(spec.VMID, &instance{process: process, api: api, state: vmm.Running}), nil
}

// Restore starts the jailed process, links the clone's state, memory and disk overlay in under the names the
// snapshot recorded and loads it. With LazyMemory, a page server answers the guest's page faults instead.
func (driver *Driver) Restore(ctx context.Context, spec vmm.RestoreSpec) (vmm.VM, error) {
	process, api, err := driver.startJailed(ctx, spec.VMID, spec.VCPUs, spec.MemoryMiB, spec.Namespace)
	if err != nil {
		return vmm.VM{}, err
	}
	files := []jailFile{{name: jailState, hostPath: spec.StatePath},
		{name: jailDisk, hostPath: spec.DiskOverlayPath, writable: true}}
	memoryBackend := jailMemory
	if spec.LazyMemory == nil {
		files = append(files, jailFile{name: jailMemory, hostPath: spec.MemoryPath})
	}
	if err := driver.linkIntoJail(spec.VMID, files); err != nil {
		driver.abandon(spec.VMID, process)
		return vmm.VM{}, err
	}
	var pages *pageServer
	if spec.LazyMemory != nil {
		pages, err = driver.startPageServer(spec.VMID, spec.LazyMemory)
		if err != nil {
			driver.abandon(spec.VMID, process)
			return vmm.VM{}, err
		}
		memoryBackend = jailPageSocket
	}
	if err := api.LoadSnapshot(ctx, jailState, memoryBackend, pages != nil, spec.Resume); err != nil {
		if pages != nil {
			pages.stop()
		}
		driver.abandon(spec.VMID, process)
		return vmm.VM{}, fmt.Errorf("load snapshot into %s: %w", spec.VMID, err)
	}
	state := vmm.Paused
	if spec.Resume {
		state = vmm.Running
	}
	return driver.track(spec.VMID, &instance{process: process, api: api, state: state, pages: pages}), nil
}

// startPageServer listens on the jail's page socket, which only the VM's user may connect to.
func (driver *Driver) startPageServer(vmID string, source vmm.PageSource) (*pageServer, error) {
	socketPath := filepath.Join(driver.JailRoot(vmID), jailPageSocket)
	pages, err := listenForPages(socketPath, source)
	if err != nil {
		return nil, err
	}
	if err := driver.chown(socketPath, driver.config.UID, driver.config.GID); err != nil {
		pages.stop()
		return nil, err
	}
	go pages.run()
	return pages, nil
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

// Snapshot pauses the VM (Firecracker requires it), writes a Full or Diff snapshot inside the jail and moves the
// files out to the paths asked for.
func (driver *Driver) Snapshot(ctx context.Context, vmID string, kind vmm.SnapshotKind, statePath,
	memoryPath string) (vmm.SnapshotRef, error) {
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
	if err := target.api.CreateSnapshot(ctx, snapshotType, jailCheckpointState, jailCheckpointMemory); err != nil {
		return vmm.SnapshotRef{}, err
	}
	root := driver.JailRoot(vmID)
	for jailName, hostPath := range map[string]string{jailCheckpointState: statePath, jailCheckpointMemory: memoryPath} {
		if err := os.Rename(filepath.Join(root, jailName), hostPath); err != nil {
			return vmm.SnapshotRef{}, fmt.Errorf("move the snapshot of %s out of its jail: %w", vmID, err)
		}
	}
	return vmm.SnapshotRef{StatePath: statePath, MemoryPath: memoryPath, Kind: kind}, nil
}

// VMMVersion is the Firecracker version the VM's snapshots are bound to.
func (driver *Driver) VMMVersion(ctx context.Context, vmID string) (string, error) {
	target, err := driver.lookup(vmID)
	if err != nil {
		return "", err
	}
	info, err := target.api.InstanceInfo(ctx)
	return info.VMMVersion, err
}

// Destroy kills the process, stops its page server and removes the jail.
func (driver *Driver) Destroy(_ context.Context, vmID string) error {
	target, err := driver.lookup(vmID)
	if err != nil {
		return err
	}
	killErr := vmm.Stop(target.process)
	var pageErr error
	if target.pages != nil {
		pageErr = target.pages.stop()
	}
	removeErr := os.RemoveAll(filepath.Dir(driver.JailRoot(vmID)))
	driver.mu.Lock()
	delete(driver.vmsByID, vmID)
	driver.mu.Unlock()
	return errors.Join(killErr, pageErr, removeErr)
}
