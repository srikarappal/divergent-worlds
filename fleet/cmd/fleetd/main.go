// Command fleetd serves the env API over one host's node agent (DESIGN.md sections 5 and 6.7).
//
// A JSON config names the host, its pool and capacity, the admission limits and
// the VMM driver: firecracker for the Linux pool, qemu_templated for the Windows
// pool, or docker so a laptop without KVM runs the same loop (docs/TASKS.md
// build decision 4). Windows guests are reached at the hypervisor and through
// their in session helper (DESIGN.md 6.3). Frames land in the
// object store directory that experimentd serves them from. With a snapshot
// store, leases name snapshots by digest and the host pulls them to its cache;
// fleetd builds base snapshots from image builds and sweeps unpinned snapshots
// on a schedule (DESIGN.md 6.2). A warm pool keeps restored, paused envs of the
// snapshots leases keep using (DESIGN.md 6.7). On shutdown every env on the host
// is destroyed so no VM or container outlives its leases.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"slices"
	"syscall"
	"time"

	"rlworld/fleet/internal/admission"
	"rlworld/fleet/internal/clock"
	"rlworld/fleet/internal/fleetd"
	"rlworld/fleet/internal/guest"
	"rlworld/fleet/internal/hostexec"
	"rlworld/fleet/internal/lease"
	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/scheduler"
	"rlworld/fleet/internal/snapshot"
	"rlworld/fleet/internal/vmm"
	"rlworld/fleet/internal/vmm/docker"
	"rlworld/fleet/internal/vmm/firecracker"
	"rlworld/fleet/internal/vmm/qemu"
)

const (
	guestAgentPort     = 5005
	guestVNCPort       = 5901
	reaperInterval     = 5 * time.Second
	shutdownDeadline   = 30 * time.Second
	collectionInterval = time.Hour
	snapshotWorkers    = 8
	defaultWarmPeriod  = 10 * time.Second
)

type hostConfig struct {
	ID            string   `json:"id"`
	Pool          string   `json:"pool"`
	CPUMillicores int      `json:"cpu_millicores"`
	MemoryMiB     int      `json:"memory_mib"`
	GPUs          int      `json:"gpus"`
	Licenses      []string `json:"licenses"`
}

type budgetConfig struct {
	TokensPerMinute    int `json:"tokens_per_minute"`
	TokensPerEnvMinute int `json:"tokens_per_env_minute"`
}

type dockerConfig struct {
	Binary              string   `json:"binary"`
	SecurityOptions     []string `json:"security_options"`
	ShmSizeMiB          int      `json:"shm_size_mib"`
	ReadyTimeoutSeconds int      `json:"ready_timeout_seconds"`
	SnapshotRepository  string   `json:"snapshot_repository"`
}

type firecrackerConfig struct {
	JailerPath      string `json:"jailer_path"`
	FirecrackerPath string `json:"firecracker_path"`
	ChrootBaseDir   string `json:"chroot_base_dir"`
	UID             int    `json:"uid"`
	GID             int    `json:"gid"`
	CPUTemplatePath string `json:"cpu_template_path"`
	BootArgs        string `json:"boot_args"`
}

// qemuConfig runs the Windows pool by VM templating; the run dir holds each VM's sockets and last screendump.
type qemuConfig struct {
	QEMUPath    string `json:"qemu_path"`
	RunDir      string `json:"run_dir"`
	MachineType string `json:"machine_type"`
	CPUModel    string `json:"cpu_model"`
}

// snapshotConfig turns on the snapshot store. The cache, the jails and the overlays share one filesystem; the
// binding fields are what this pool's snapshots are bound to (DESIGN.md 6.1.6).
type snapshotConfig struct {
	StoreDir      string `json:"store_dir"`
	CacheDir      string `json:"cache_dir"`
	VMMVersion    string `json:"vmm_version"`
	MachineType   string `json:"machine_type"`
	CPUModel      string `json:"cpu_model"`
	LazyMemory    bool   `json:"lazy_memory"`
	RetentionDays int    `json:"retention_days"`
	GraceHours    int    `json:"grace_hours"`
	// SlowWorldSeconds is how long a world may take to load before it gets a world snapshot; zero means 5.
	SlowWorldSeconds float64 `json:"slow_world_seconds"`
}

// warmPoolConfig keeps per_snapshot restored, paused envs of every snapshot a lease used on this host within
// idle_minutes; zero per_snapshot turns warm pools off, as Linux hosts whose restores are fast leave it.
type warmPoolConfig struct {
	PerSnapshot     int `json:"per_snapshot"`
	IdleMinutes     int `json:"idle_minutes"`
	IntervalSeconds int `json:"interval_seconds"`
}

type config struct {
	Listen              string                     `json:"listen"`
	Driver              string                     `json:"driver"`
	Host                hostConfig                 `json:"host"`
	Capacity            int                        `json:"capacity"`
	Reserved            map[admission.Priority]int `json:"reserved"`
	TenantQuotas        map[string]int             `json:"tenant_quotas"`
	DefaultTenantQuota  int                        `json:"default_tenant_quota"`
	EndpointBudgets     map[string]budgetConfig    `json:"endpoint_budgets"`
	LeaseTTLSeconds     int                        `json:"lease_ttl_seconds"`
	StateDir            string                     `json:"state_dir"`
	ObjectStoreDir      string                     `json:"object_store_dir"`
	GuestTimeoutSeconds int                        `json:"guest_timeout_seconds"`
	Docker              dockerConfig               `json:"docker"`
	Firecracker         firecrackerConfig          `json:"firecracker"`
	QEMU                qemuConfig                 `json:"qemu"`
	Snapshots           snapshotConfig             `json:"snapshots"`
	WarmPool            warmPoolConfig             `json:"warm_pool"`
}

// tools are the host side effects; tests swap in fakes.
type tools struct {
	commands  nodeagent.CommandRunner
	processes vmm.ProcessRunner
	docker    docker.Commander
}

type fleet struct {
	handler   http.Handler
	leases    *lease.Manager
	node      *nodeagent.Agent
	service   *fleetd.Service
	snapshots bool
}

func loadConfig(path string) (config, error) {
	var loaded config
	raw, err := os.ReadFile(path)
	if err != nil {
		return loaded, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&loaded); err != nil {
		return loaded, fmt.Errorf("%s: %w", path, err)
	}
	if loaded.LeaseTTLSeconds == 0 {
		loaded.LeaseTTLSeconds = 300
	}
	if loaded.GuestTimeoutSeconds == 0 {
		loaded.GuestTimeoutSeconds = 30
	}
	return loaded, loaded.validate()
}

func (loaded config) validate() error {
	var problems []error
	if loaded.Listen == "" {
		problems = append(problems, errors.New("listen is required"))
	}
	if !slices.Contains([]string{"docker", "firecracker", "qemu_templated"}, loaded.Driver) {
		problems = append(problems, fmt.Errorf("driver %q is not one of docker, firecracker or qemu_templated",
			loaded.Driver))
	}
	if loaded.Driver == "qemu_templated" && (loaded.QEMU.QEMUPath == "" || loaded.QEMU.RunDir == "" ||
		loaded.QEMU.MachineType == "" || loaded.QEMU.CPUModel == "") {
		problems = append(problems, errors.New("qemu needs qemu_path, run_dir, machine_type and cpu_model"))
	}
	if loaded.Host.ID == "" || loaded.Host.Pool == "" {
		problems = append(problems, errors.New("host needs an id and a pool"))
	}
	if loaded.Host.CPUMillicores <= 0 || loaded.Host.MemoryMiB <= 0 {
		problems = append(problems, errors.New("host needs cpu_millicores and memory_mib"))
	}
	if loaded.Capacity <= 0 {
		problems = append(problems, errors.New("capacity must be positive"))
	}
	if loaded.StateDir == "" || loaded.ObjectStoreDir == "" {
		problems = append(problems, errors.New("state_dir and object_store_dir are required"))
	}
	if loaded.Snapshots.StoreDir != "" {
		if loaded.Driver == "docker" {
			problems = append(problems, errors.New("the snapshot store serves VM pools; docker images are snapshots"))
		}
		if loaded.Snapshots.CacheDir == "" || loaded.Snapshots.CPUModel == "" || loaded.Snapshots.VMMVersion == "" {
			problems = append(problems, errors.New("snapshots need cache_dir, vmm_version and cpu_model"))
		}
		if loaded.Driver == "qemu_templated" && (loaded.Snapshots.MachineType != loaded.QEMU.MachineType ||
			loaded.Snapshots.CPUModel != loaded.QEMU.CPUModel) {
			problems = append(problems, errors.New("snapshots must bind the machine type and CPU model qemu runs"))
		}
	}
	return errors.Join(problems...)
}

func (loaded config) admissionConfig() admission.Config {
	budgets := map[string]admission.EndpointBudget{}
	for endpoint, budget := range loaded.EndpointBudgets {
		budgets[endpoint] = admission.EndpointBudget{TokensPerMinute: budget.TokensPerMinute,
			TokensPerEnvMinute: budget.TokensPerEnvMinute}
	}
	return admission.Config{Capacity: loaded.Capacity, Reserved: loaded.Reserved, TenantQuotas: loaded.TenantQuotas,
		DefaultTenantQuota: loaded.DefaultTenantQuota, EndpointBudgets: budgets}
}

func (loaded config) schedulerHost() scheduler.Host {
	licenses := map[string]bool{}
	for _, license := range loaded.Host.Licenses {
		licenses[license] = true
	}
	return scheduler.Host{ID: loaded.Host.ID, Pool: loaded.Host.Pool, HeldLicenses: licenses,
		Capacity: scheduler.Resources{CPUMillicores: loaded.Host.CPUMillicores, MemoryMiB: loaded.Host.MemoryMiB,
			GPUs: loaded.Host.GPUs}}
}

// newDriver returns the driver and the guest channel that reaches its guests: the guest agent and its VNC relay
// on Linux pools; the hypervisor, the in session helper and QEMU's VNC server on the Windows pool.
func newDriver(loaded config, hostTools tools, frames guest.FrameStore) (vmm.Driver, nodeagent.GuestChannel) {
	timeout := time.Duration(loaded.GuestTimeoutSeconds) * time.Second
	switch loaded.Driver {
	case "docker":
		driver := docker.NewDriver(docker.Config{Binary: loaded.Docker.Binary, AgentPort: guestAgentPort,
			SecurityOptions: loaded.Docker.SecurityOptions, ShmSizeMiB: loaded.Docker.ShmSizeMiB,
			ReadyTimeout:       time.Duration(loaded.Docker.ReadyTimeoutSeconds) * time.Second,
			SnapshotRepository: loaded.Docker.SnapshotRepository}, hostTools.docker)
		return driver, guest.NewChannel(guest.TokenDialer{Endpoints: driver},
			guest.TokenDialer{Endpoints: driver, Greeting: "VNC"}, frames, timeout)
	case "qemu_templated":
		driver := qemu.NewDriver(qemu.Config{QEMUPath: loaded.QEMU.QEMUPath, MachineType: loaded.QEMU.MachineType,
			CPUModel: loaded.QEMU.CPUModel, RunDir: loaded.QEMU.RunDir}, hostTools.processes, nil)
		return driver, guest.NewWindowsChannel(driver, frames, timeout, time.Sleep)
	}
	driver := firecracker.NewDriver(firecracker.Config{JailerPath: loaded.Firecracker.JailerPath,
		FirecrackerPath: loaded.Firecracker.FirecrackerPath, ChrootBaseDir: loaded.Firecracker.ChrootBaseDir,
		UID: loaded.Firecracker.UID, GID: loaded.Firecracker.GID, CPUTemplatePath: loaded.Firecracker.CPUTemplatePath,
		BootArgs: loaded.Firecracker.BootArgs}, hostTools.processes, nil)
	return driver, guest.NewChannel(guest.VsockDialer{SocketPath: driver.VsockPath, Port: guestAgentPort},
		guest.VsockDialer{SocketPath: driver.VsockPath, Port: guestVNCPort}, frames, timeout)
}

func newFleet(loaded config, hostTools tools) (*fleet, error) {
	overlayDir := filepath.Join(loaded.StateDir, "overlays")
	checkpointDir := filepath.Join(loaded.StateDir, "checkpoints")
	directories := []string{overlayDir, checkpointDir, loaded.ObjectStoreDir}
	if loaded.Driver == "qemu_templated" {
		directories = append(directories, loaded.QEMU.RunDir)
	}
	for _, directory := range directories {
		if err := os.MkdirAll(directory, 0o750); err != nil {
			return nil, err
		}
	}
	controller, err := admission.NewController(loaded.admissionConfig())
	if err != nil {
		return nil, err
	}
	leases := lease.NewManager(controller, clock.Real{}, time.Duration(loaded.LeaseTTLSeconds)*time.Second)
	placement := scheduler.New()
	placement.UpsertHost(loaded.schedulerHost())
	driver, channel := newDriver(loaded, hostTools, guest.NewFilesystemFrames(loaded.ObjectStoreDir))
	nodeConfig := nodeagent.Config{HostID: loaded.Host.ID, OverlayDir: overlayDir}
	settings, snapshotsOn := loaded.snapshotSettings()
	if snapshotsOn {
		for _, directory := range []string{loaded.Snapshots.StoreDir, loaded.Snapshots.CacheDir} {
			if err := os.MkdirAll(directory, 0o750); err != nil {
				return nil, err
			}
		}
		nodeConfig.Snapshots = snapshot.NewCache(settings.Store, loaded.Snapshots.CacheDir, settings.Compatibility,
			snapshotWorkers)
		nodeConfig.LazyMemory = loaded.Snapshots.LazyMemory
	}
	node := nodeagent.New(nodeConfig, driver, channel, hostTools.commands, clock.Real{}, nil)
	service := fleetd.NewService(leases, placement, map[string]fleetd.Node{loaded.Host.ID: node}, checkpointDir)
	if snapshotsOn {
		service.EnableSnapshots(settings)
		if err := service.MarkCachedSnapshots(); err != nil {
			return nil, err
		}
	}
	if loaded.WarmPool.PerSnapshot > 0 {
		service.EnableWarmPools(fleetd.WarmPolicy{PerSnapshot: loaded.WarmPool.PerSnapshot,
			IdleAfter: time.Duration(max(loaded.WarmPool.IdleMinutes, 1)) * time.Minute})
	}
	return &fleet{handler: fleetd.NewHandler(service), leases: leases, node: node, service: service,
		snapshots: snapshotsOn}, nil
}

// snapshotSettings binds this host's snapshots to its pool, driver, VMM version and CPU model; builds work inside
// the cache directory so their files never cross filesystems.
func (loaded config) snapshotSettings() (fleetd.SnapshotSettings, bool) {
	if loaded.Snapshots.StoreDir == "" {
		return fleetd.SnapshotSettings{}, false
	}
	binding := snapshot.Compatibility{Pool: loaded.Host.Pool, VMM: loaded.Driver,
		VMMVersion: loaded.Snapshots.VMMVersion, MachineType: loaded.Snapshots.MachineType,
		CPUModel: loaded.Snapshots.CPUModel}
	retention := snapshot.RetentionPolicy{MinAge: time.Duration(max(loaded.Snapshots.RetentionDays, 1)) * 24 *
		time.Hour, Grace: time.Duration(max(loaded.Snapshots.GraceHours, 1)) * time.Hour}
	return fleetd.SnapshotSettings{Store: snapshot.DirStore{Root: loaded.Snapshots.StoreDir}, Compatibility: binding,
		WorkDir: loaded.Snapshots.CacheDir, Retention: retention,
		SlowWorld: time.Duration(loaded.Snapshots.SlowWorldSeconds * float64(time.Second))}, true
}

// warmPeriod is how often warm pools are topped up and cooled.
func (loaded config) warmPeriod() time.Duration {
	if loaded.WarmPool.IntervalSeconds > 0 {
		return time.Duration(loaded.WarmPool.IntervalSeconds) * time.Second
	}
	return defaultWarmPeriod
}

// maintainWarmPools keeps the warm pools in shape every interval until fleetd stops.
func maintainWarmPools(ctx context.Context, service *fleetd.Service, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := service.MaintainWarmPools(ctx); err != nil {
				log.Printf("warm pool upkeep: %v", err)
			}
		}
	}
}

// collectSnapshots applies the retention policy every interval until fleetd stops.
func collectSnapshots(ctx context.Context, service *fleetd.Service, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			report, err := service.CollectSnapshots(ctx)
			if err != nil {
				log.Printf("snapshot collection failed: %v", err)
			} else if len(report.Removed) > 0 {
				log.Printf("snapshot collection removed %d snapshots and %d chunks, freeing %d bytes",
					len(report.Removed), report.RemovedChunks, report.FreedBytes)
			}
		}
	}
}

// destroyAll tears down every env still on the host.
func (built *fleet) destroyAll(ctx context.Context) error {
	var problems []error
	for _, info := range built.node.Envs() {
		problems = append(problems, built.node.Destroy(ctx, info.EnvID))
	}
	return errors.Join(problems...)
}

func serve(server *http.Server, serveErrors chan<- error) {
	if err := server.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		serveErrors <- err
	}
}

func run(configPath string) error {
	loaded, err := loadConfig(configPath)
	if err != nil {
		return err
	}
	built, err := newFleet(loaded, tools{commands: hostexec.Commands{}, processes: hostexec.Processes{},
		docker: docker.ExecCommander{}})
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go built.leases.RunReaper(ctx, reaperInterval)
	if built.snapshots {
		go collectSnapshots(ctx, built.service, collectionInterval)
	}
	if loaded.WarmPool.PerSnapshot > 0 {
		go maintainWarmPools(ctx, built.service, loaded.warmPeriod())
	}
	server := &http.Server{Addr: loaded.Listen, Handler: built.handler, ReadHeaderTimeout: 10 * time.Second}
	serveErrors := make(chan error, 1)
	go serve(server, serveErrors)
	log.Printf("fleetd serves %s with the %s driver for host %s", loaded.Listen, loaded.Driver, loaded.Host.ID)
	select {
	case err := <-serveErrors:
		return err
	case <-ctx.Done():
	}
	shutdownContext, cancel := context.WithTimeout(context.Background(), shutdownDeadline)
	defer cancel()
	return errors.Join(server.Shutdown(shutdownContext), built.destroyAll(shutdownContext))
}

func main() {
	configPath := flag.String("config", "fleetd.json", "fleetd config file")
	flag.Parse()
	if err := run(*configPath); err != nil {
		log.Fatal(err)
	}
}
