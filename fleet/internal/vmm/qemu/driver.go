package qemu

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strconv"
	"sync"
	"time"

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
}

// Driver restores clones by VM templating: guest RAM is a file mapped private, so clones share
// untouched pages, and only device state is loaded through the x-ignore-shared migration capability.
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

// QMPSocketPath is where one clone's QMP socket lives.
func (driver *Driver) QMPSocketPath(vmID string) string {
	return filepath.Join(driver.config.RunDir, vmID+".qmp")
}

// TemplatingArgs builds the QEMU command line for one clone, run inside its network namespace.
func (driver *Driver) TemplatingArgs(spec vmm.RestoreSpec) []string {
	memorySize := strconv.Itoa(spec.MemoryMiB) + "M"
	argv := []string{}
	if spec.Namespace != "" {
		argv = append(argv, "ip", "netns", "exec", spec.Namespace)
	}
	return append(argv,
		driver.config.QEMUPath,
		"-name", spec.VMID,
		"-machine", driver.config.MachineType+",accel=kvm,memory-backend=guest_ram",
		"-cpu", driver.config.CPUModel,
		"-smp", strconv.Itoa(spec.VCPUs),
		"-m", memorySize,
		"-object", "memory-backend-file,id=guest_ram,size="+memorySize+",mem-path="+spec.MemoryPath+",share=off",
		"-drive", "file="+spec.DiskOverlayPath+",if=virtio,format=qcow2,cache=none",
		"-nic", "tap,ifname=tap0,script=no,downscript=no,model=virtio-net-pci",
		"-device", "qemu-xhci", "-device", "usb-tablet",
		"-vga", "std", "-display", "none",
		"-qmp", "unix:"+driver.QMPSocketPath(spec.VMID)+",server=on,wait=off",
		"-incoming", "defer",
	)
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
		_ = process.Kill()
		return vmm.VM{}, err
	}
	restoreSteps := []struct {
		command   string
		arguments any
	}{
		{"migrate-set-capabilities", map[string]any{"capabilities": []map[string]any{
			{"capability": "x-ignore-shared", "state": true}}}},
		{"migrate-incoming", map[string]any{"uri": "file:" + spec.StatePath}},
	}
	for _, step := range restoreSteps {
		if _, err := channel.Execute(ctx, step.command, step.arguments); err != nil {
			_ = channel.Close()
			_ = process.Kill()
			return vmm.VM{}, fmt.Errorf("%s for %s: %w", step.command, spec.VMID, err)
		}
	}
	if err := driver.waitForMigration(ctx, channel); err != nil {
		_ = channel.Close()
		_ = process.Kill()
		return vmm.VM{}, err
	}
	state := vmm.Paused
	if spec.Resume {
		if _, err := channel.Execute(ctx, "cont", nil); err != nil {
			_ = channel.Close()
			_ = process.Kill()
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

// Snapshot pauses and migrates the full state to a file; private RAM has diverged, so all of it is written.
func (driver *Driver) Snapshot(ctx context.Context, vmID string, kind vmm.SnapshotKind, statePath, _ string) (vmm.SnapshotRef, error) {
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
	if _, err := target.qmp.Execute(ctx, "migrate", map[string]any{"uri": "file:" + statePath}); err != nil {
		return vmm.SnapshotRef{}, err
	}
	if err := driver.waitForMigration(ctx, target.qmp); err != nil {
		return vmm.SnapshotRef{}, err
	}
	return vmm.SnapshotRef{StatePath: statePath, Kind: vmm.Full}, nil
}

// Destroy asks QEMU to quit, then kills the process either way.
func (driver *Driver) Destroy(ctx context.Context, vmID string) error {
	target, err := driver.lookup(vmID)
	if err != nil {
		return err
	}
	_, _ = target.qmp.Execute(ctx, "quit", nil)
	closeErr := target.qmp.Close()
	killErr := target.process.Kill()
	driver.mu.Lock()
	delete(driver.vmsByID, vmID)
	driver.mu.Unlock()
	return errors.Join(closeErr, killErr)
}

// CaptureScreen writes the framebuffer as PNG, outside the guest's reach.
func (driver *Driver) CaptureScreen(ctx context.Context, vmID, outputPath string) error {
	return driver.execute(ctx, vmID, "screendump", map[string]any{"filename": outputPath, "format": "png"})
}

// SendPointer moves the absolute pointer and optionally presses or releases a button.
func (driver *Driver) SendPointer(ctx context.Context, vmID string, x, y, screenWidth, screenHeight int, button string, down bool) error {
	events := PointerEvents(x, y, screenWidth, screenHeight, button, down)
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
