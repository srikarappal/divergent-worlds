// Command fleetd serves the env API over one host's node agent (DESIGN.md sections 5 and 6.7).
//
// A JSON config names the host, its pool and capacity, the admission limits and
// the VMM driver: firecracker for the Linux pool, or docker so a laptop without
// KVM runs the same loop (docs/TASKS.md build decision 4). Frames land in the
// object store directory that experimentd serves them from. On shutdown every
// env on the host is destroyed so no VM or container outlives its leases.
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
	"rlworld/fleet/internal/vmm"
	"rlworld/fleet/internal/vmm/docker"
	"rlworld/fleet/internal/vmm/firecracker"
)

const (
	guestAgentPort   = 5005
	guestVNCPort     = 5901
	reaperInterval   = 5 * time.Second
	shutdownDeadline = 30 * time.Second
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
	UseUffd         bool   `json:"use_uffd"`
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
}

// tools are the host side effects; tests swap in fakes.
type tools struct {
	commands  nodeagent.CommandRunner
	processes vmm.ProcessRunner
	docker    docker.Commander
}

type fleet struct {
	handler http.Handler
	leases  *lease.Manager
	node    *nodeagent.Agent
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
	if loaded.Driver != "docker" && loaded.Driver != "firecracker" {
		problems = append(problems, fmt.Errorf("driver %q is not one of docker or firecracker", loaded.Driver))
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

// newDriver returns the driver and how the guest channel reaches its guests' agents and VNC relays.
func newDriver(loaded config, hostTools tools) (vmm.Driver, guest.Dialer, guest.Dialer) {
	if loaded.Driver == "docker" {
		driver := docker.NewDriver(docker.Config{Binary: loaded.Docker.Binary, AgentPort: guestAgentPort,
			SecurityOptions: loaded.Docker.SecurityOptions, ShmSizeMiB: loaded.Docker.ShmSizeMiB,
			ReadyTimeout:       time.Duration(loaded.Docker.ReadyTimeoutSeconds) * time.Second,
			SnapshotRepository: loaded.Docker.SnapshotRepository}, hostTools.docker)
		return driver, guest.TokenDialer{Endpoints: driver}, guest.TokenDialer{Endpoints: driver, Greeting: "VNC"}
	}
	driver := firecracker.NewDriver(firecracker.Config{JailerPath: loaded.Firecracker.JailerPath,
		FirecrackerPath: loaded.Firecracker.FirecrackerPath, ChrootBaseDir: loaded.Firecracker.ChrootBaseDir,
		UID: loaded.Firecracker.UID, GID: loaded.Firecracker.GID, UseUffd: loaded.Firecracker.UseUffd},
		hostTools.processes, nil)
	return driver, guest.VsockDialer{SocketPath: driver.VsockPath, Port: guestAgentPort},
		guest.VsockDialer{SocketPath: driver.VsockPath, Port: guestVNCPort}
}

func newFleet(loaded config, hostTools tools) (*fleet, error) {
	overlayDir := filepath.Join(loaded.StateDir, "overlays")
	checkpointDir := filepath.Join(loaded.StateDir, "checkpoints")
	for _, directory := range []string{overlayDir, checkpointDir, loaded.ObjectStoreDir} {
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
	driver, dialer, vncDialer := newDriver(loaded, hostTools)
	channel := guest.NewChannel(dialer, vncDialer, guest.NewFilesystemFrames(loaded.ObjectStoreDir),
		time.Duration(loaded.GuestTimeoutSeconds)*time.Second)
	node := nodeagent.New(nodeagent.Config{HostID: loaded.Host.ID, OverlayDir: overlayDir}, driver, channel,
		hostTools.commands, clock.Real{}, nil)
	service := fleetd.NewService(leases, placement, map[string]fleetd.Node{loaded.Host.ID: node}, checkpointDir)
	return &fleet{handler: fleetd.NewHandler(service), leases: leases, node: node}, nil
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
