package qemu

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"rlworld/fleet/internal/netplan"
	"rlworld/fleet/internal/vmm"
)

// Config locates QEMU and pins what snapshots depend on: machine type and CPU model.
type Config struct {
	QEMUPath           string
	MachineType        string
	CPUModel           string
	RunDir             string
	SocketWaitTimeout  time.Duration
	MigrationPollDelay time.Duration
}

// ignoreSharedCapability keeps guest RAM out of the migration stream: templates and clones share the RAM file.
var ignoreSharedCapability = map[string]any{"capabilities": []map[string]any{
	{"capability": "x-ignore-shared", "state": true}}}

// Dialer opens the QMP channel; tests swap in a fake.
type Dialer func(ctx context.Context, socketPath string) (QMP, error)

// DialQMPChannel is the default Dialer: a real QMP client.
func DialQMPChannel(ctx context.Context, socketPath string) (QMP, error) {
	return DialQMP(ctx, socketPath)
}

type instance struct {
	process vmm.Process
	qmp     QMP
	state   vmm.State
	// template VMs were cold booted with RAM shared into memoryPath, which their snapshot keeps as its memory.
	template   bool
	memoryPath string
}

// Driver restores clones by VM templating: guest RAM is a file mapped private, so clones share
// untouched pages; only device state is loaded through the x-ignore-shared migration capability.
type Driver struct {
	mu      sync.Mutex
	config  Config
	runner  vmm.ProcessRunner
	dial    Dialer
	vmsByID map[string]*instance
}

// NewDriver returns a driver; a nil dialer uses the real QMP client.
func NewDriver(config Config, runner vmm.ProcessRunner, dial Dialer) *Driver {
	if dial == nil {
		dial = DialQMPChannel
	}
	if config.SocketWaitTimeout <= 0 {
		config.SocketWaitTimeout = 10 * time.Second
	}
	if config.MigrationPollDelay <= 0 {
		config.MigrationPollDelay = 20 * time.Millisecond
	}
	return &Driver{config: config, runner: runner, dial: dial, vmsByID: map[string]*instance{}}
}

// Name identifies the driver.
func (driver *Driver) Name() string {
	return "qemu_templated"
}

// HelperPortName is the virtio serial port the in session helper of a Windows guest serves.
const HelperPortName = "org.rlworld.helper.0"

// GuestAgentPortName is the virtio serial port of the QEMU guest agent, which sets the guest clock.
const GuestAgentPortName = "org.qemu.guest_agent.0"

// QMPSocketPath is where one clone's QMP socket lives.
func (driver *Driver) QMPSocketPath(vmID string) string {
	return filepath.Join(driver.config.RunDir, vmID+".qmp")
}

// HelperSocketPath is the host end of the in session helper's virtio serial port.
func (driver *Driver) HelperSocketPath(vmID string) string {
	return filepath.Join(driver.config.RunDir, vmID+".helper")
}

// GuestAgentSocketPath is the host end of the QEMU guest agent's virtio serial port.
func (driver *Driver) GuestAgentSocketPath(vmID string) string {
	return filepath.Join(driver.config.RunDir, vmID+".qga")
}

// VNCSocketPath is where QEMU serves the VM's screen for operator takeover.
func (driver *Driver) VNCSocketPath(vmID string) string {
	return filepath.Join(driver.config.RunDir, vmID+".vnc")
}

// ScreenPath is where screendumps of the VM land before the host reads them.
func (driver *Driver) ScreenPath(vmID string) string {
	return filepath.Join(driver.config.RunDir, vmID+".screen.png")
}

func serialPortArgs(id, socketPath, portName string) []string {
	return []string{"-chardev", "socket,id=" + id + ",path=" + socketPath + ",server=on,wait=off",
		"-device", "virtserialport,chardev=" + id + ",name=" + portName}
}

// machineArgs is the command line a template and its clones share, so device state migrates between them. The
// template writes guest RAM through to its file; clones map that file private and wait for the device state.
// Screens and input stay at the hypervisor (QMP and a USB tablet); the in session helper and the QEMU guest agent
// each get a virtio serial port; QEMU serves VNC on a socket for takeover. Every QEMU process gets a fresh VM
// generation id, so Windows reseeds its RNG and renews what it ties to the VM's identity in every clone.
func (driver *Driver) machineArgs(vmID, namespace string, vcpus, memoryMiB int, memoryPath, diskPath string,
	template bool) []string {
	memorySize := strconv.Itoa(memoryMiB) + "M"
	share := "off"
	if template {
		share = "on"
	}
	argv := []string{}
	if namespace != "" {
		argv = append(argv, "ip", "netns", "exec", namespace)
	}
	argv = append(argv,
		driver.config.QEMUPath,
		"-name", vmID,
		"-machine", driver.config.MachineType+",accel=kvm,memory-backend=guest_ram",
		"-cpu", driver.config.CPUModel,
		"-smp", strconv.Itoa(vcpus),
		"-m", memorySize,
		"-object", "memory-backend-file,id=guest_ram,size="+memorySize+",mem-path="+memoryPath+",share="+share,
		"-drive", "file="+diskPath+",if=virtio,format=qcow2,cache=none",
		"-nic", "tap,ifname="+netplan.TapName+",script=no,downscript=no,model=virtio-net-pci,mac="+netplan.GuestMAC,
		"-device", "qemu-xhci", "-device", "usb-tablet",
		"-vga", "std", "-display", "none",
		"-vnc", "unix:"+driver.VNCSocketPath(vmID),
		"-device", "vmgenid,guid=auto",
		"-device", "virtio-serial-pci",
	)
	argv = append(argv, serialPortArgs("helper", driver.HelperSocketPath(vmID), HelperPortName)...)
	argv = append(argv, serialPortArgs("qga", driver.GuestAgentSocketPath(vmID), GuestAgentPortName)...)
	argv = append(argv, "-qmp", "unix:"+driver.QMPSocketPath(vmID)+",server=on,wait=off")
	if !template {
		argv = append(argv, "-incoming", "defer")
	}
	return argv
}

// TemplatingArgs builds the QEMU command line for one clone, run inside its network namespace.
func (driver *Driver) TemplatingArgs(spec vmm.RestoreSpec) []string {
	return driver.machineArgs(spec.VMID, spec.Namespace, spec.VCPUs, spec.MemoryMiB, spec.MemoryPath,
		spec.DiskOverlayPath, false)
}

// TemplateArgs builds the command line of the template boot the snapshot builder takes a base snapshot of.
func (driver *Driver) TemplateArgs(spec vmm.BootSpec) []string {
	return driver.machineArgs(spec.VMID, spec.Namespace, spec.VCPUs, spec.MemoryMiB, spec.MemoryPath, spec.DiskPath,
		true)
}

// Boot cold boots a template from its disk with guest RAM shared into spec.MemoryPath.
func (driver *Driver) Boot(ctx context.Context, spec vmm.BootSpec) (vmm.VM, error) {
	process, err := driver.runner.Start(ctx, driver.TemplateArgs(spec))
	if err != nil {
		return vmm.VM{}, fmt.Errorf("start qemu for %s: %w", spec.VMID, err)
	}
	channel, err := driver.dialWhenReady(ctx, driver.QMPSocketPath(spec.VMID))
	if err != nil {
		vmm.Stop(process)
		return vmm.VM{}, err
	}
	driver.mu.Lock()
	driver.vmsByID[spec.VMID] = &instance{process: process, qmp: channel, state: vmm.Running, template: true,
		memoryPath: spec.MemoryPath}
	driver.mu.Unlock()
	return vmm.VM{ID: spec.VMID, State: vmm.Running, Driver: driver.Name()}, nil
}

func (driver *Driver) dialWhenReady(ctx context.Context, socketPath string) (QMP, error) {
	deadline := time.Now().Add(driver.config.SocketWaitTimeout)
	for {
		channel, err := driver.dial(ctx, socketPath)
		if err == nil {
			return channel, nil
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("qmp socket %s not ready: %w", socketPath, err)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(driver.config.MigrationPollDelay):
		}
	}
}

func (driver *Driver) waitForMigration(ctx context.Context, channel QMP) error {
	for {
		reply, err := channel.Execute(ctx, "query-migrate", nil)
		if err != nil {
			return err
		}
		var migration struct {
			Status      string `json:"status"`
			ErrorDetail string `json:"error-desc"`
		}
		if err := json.Unmarshal(reply, &migration); err != nil {
			return err
		}
		switch migration.Status {
		case "completed":
			return nil
		case "failed", "cancelled":
			return fmt.Errorf("migration %s: %s", migration.Status, migration.ErrorDetail)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(driver.config.MigrationPollDelay):
		}
	}
}

// Restore starts QEMU waiting for an incoming migration, then loads only device state from the template.
func (driver *Driver) Restore(ctx context.Context, spec vmm.RestoreSpec) (vmm.VM, error) {
	process, err := driver.runner.Start(ctx, driver.TemplatingArgs(spec))
	if err != nil {
		return vmm.VM{}, fmt.Errorf("start qemu for %s: %w", spec.VMID, err)
	}
	channel, err := driver.dialWhenReady(ctx, driver.QMPSocketPath(spec.VMID))
	if err != nil {
		vmm.Stop(process)
		return vmm.VM{}, err
	}
	restoreSteps := []struct {
		command   string
		arguments any
	}{
		{"migrate-set-capabilities", ignoreSharedCapability},
		{"migrate-incoming", map[string]any{"uri": "file:" + spec.StatePath}},
	}
	for _, step := range restoreSteps {
		if _, err := channel.Execute(ctx, step.command, step.arguments); err != nil {
			channel.Close()
			vmm.Stop(process)
			return vmm.VM{}, fmt.Errorf("%s for %s: %w", step.command, spec.VMID, err)
		}
	}
	if err := driver.waitForMigration(ctx, channel); err != nil {
		channel.Close()
		vmm.Stop(process)
		return vmm.VM{}, err
	}
	state := vmm.Paused
	if spec.Resume {
		if _, err := channel.Execute(ctx, "cont", nil); err != nil {
			channel.Close()
			vmm.Stop(process)
			return vmm.VM{}, err
		}
		state = vmm.Running
	}
	driver.mu.Lock()
	driver.vmsByID[spec.VMID] = &instance{process: process, qmp: channel, state: state}
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

func (driver *Driver) execute(ctx context.Context, vmID, command string, arguments any) error {
	target, err := driver.lookup(vmID)
	if err != nil {
		return err
	}
	_, err = target.qmp.Execute(ctx, command, arguments)
	return err
}

func (driver *Driver) setState(vmID string, state vmm.State) {
	driver.mu.Lock()
	defer driver.mu.Unlock()
	if target, found := driver.vmsByID[vmID]; found {
		target.state = state
	}
}

// Pause stops the vCPUs.
func (driver *Driver) Pause(ctx context.Context, vmID string) error {
	if err := driver.execute(ctx, vmID, "stop", nil); err != nil {
		return err
	}
	driver.setState(vmID, vmm.Paused)
	return nil
}

// Resume restarts the vCPUs.
func (driver *Driver) Resume(ctx context.Context, vmID string) error {
	if err := driver.execute(ctx, vmID, "cont", nil); err != nil {
		return err
	}
	driver.setState(vmID, vmm.Running)
	return nil
}

// Snapshot pauses and migrates the state to a file. A clone's private RAM has diverged, so all of it is written;
// a template's RAM already sits in its shared file, so only device state is written and that file is the memory.
func (driver *Driver) Snapshot(ctx context.Context, vmID string, kind vmm.SnapshotKind, statePath,
	_ string) (vmm.SnapshotRef, error) {
	if kind == vmm.Diff {
		return vmm.SnapshotRef{}, errors.New("qemu templating writes full checkpoints only")
	}
	if err := driver.Pause(ctx, vmID); err != nil {
		return vmm.SnapshotRef{}, err
	}
	target, err := driver.lookup(vmID)
	if err != nil {
		return vmm.SnapshotRef{}, err
	}
	if target.template {
		if _, err := target.qmp.Execute(ctx, "migrate-set-capabilities", ignoreSharedCapability); err != nil {
			return vmm.SnapshotRef{}, err
		}
	}
	if _, err := target.qmp.Execute(ctx, "migrate", map[string]any{"uri": "file:" + statePath}); err != nil {
		return vmm.SnapshotRef{}, err
	}
	if err := driver.waitForMigration(ctx, target.qmp); err != nil {
		return vmm.SnapshotRef{}, err
	}
	if target.template {
		return vmm.SnapshotRef{StatePath: statePath, MemoryPath: target.memoryPath, Kind: vmm.Full}, nil
	}
	return vmm.SnapshotRef{StatePath: statePath, Kind: vmm.Full}, nil
}

// VMMVersion is the QEMU version the VM's snapshots are bound to.
func (driver *Driver) VMMVersion(ctx context.Context, vmID string) (string, error) {
	target, err := driver.lookup(vmID)
	if err != nil {
		return "", err
	}
	reply, err := target.qmp.Execute(ctx, "query-version", nil)
	if err != nil {
		return "", err
	}
	var version struct {
		QEMU struct {
			Major int `json:"major"`
			Minor int `json:"minor"`
			Micro int `json:"micro"`
		} `json:"qemu"`
	}
	if err := json.Unmarshal(reply, &version); err != nil {
		return "", err
	}
	return fmt.Sprintf("%d.%d.%d", version.QEMU.Major, version.QEMU.Minor, version.QEMU.Micro), nil
}

// Destroy asks QEMU to quit, kills the process either way, then removes the VM's sockets and last screendump.
func (driver *Driver) Destroy(ctx context.Context, vmID string) error {
	target, err := driver.lookup(vmID)
	if err != nil {
		return err
	}
	target.qmp.Execute(ctx, "quit", nil)
	problems := []error{target.qmp.Close(), vmm.Stop(target.process)}
	driver.mu.Lock()
	delete(driver.vmsByID, vmID)
	driver.mu.Unlock()
	for _, path := range []string{driver.QMPSocketPath(vmID), driver.HelperSocketPath(vmID),
		driver.GuestAgentSocketPath(vmID), driver.VNCSocketPath(vmID), driver.ScreenPath(vmID)} {
		if err := os.Remove(path); err != nil && !errors.Is(err, fs.ErrNotExist) {
			problems = append(problems, err)
		}
	}
	return errors.Join(problems...)
}

// CaptureScreen writes the framebuffer as PNG, outside the guest's reach.
func (driver *Driver) CaptureScreen(ctx context.Context, vmID, outputPath string) error {
	return driver.execute(ctx, vmID, "screendump", map[string]any{"filename": outputPath, "format": "png"})
}

// SendPointer moves the absolute pointer and optionally presses or releases a button.
func (driver *Driver) SendPointer(ctx context.Context, vmID string, x, y, screenWidth, screenHeight int,
	button string, down bool) error {
	events := PointerEvents(x, y, screenWidth, screenHeight, button, down)
	return driver.execute(ctx, vmID, "input-send-event", map[string]any{"events": events})
}

// SendInput delivers input events in order through the USB tablet and keyboard.
func (driver *Driver) SendInput(ctx context.Context, vmID string, events []map[string]any) error {
	return driver.execute(ctx, vmID, "input-send-event", map[string]any{"events": events})
}

// SendKeys presses a key combo and releases it.
func (driver *Driver) SendKeys(ctx context.Context, vmID string, keys []string) error {
	events, err := KeyEvents(keys)
	if err != nil {
		return err
	}
	return driver.execute(ctx, vmID, "input-send-event", map[string]any{"events": events})
}
