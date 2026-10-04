// Package fleetd serves the env API (DESIGN.md sections 5 and 6.7): leases, steps, forks,
// checkpoints and finish, on top of admission, the lease manager, the scheduler and node agents.
package fleetd

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"path/filepath"
	"sync"
	"time"

	"rlworld/fleet/internal/admission"
	"rlworld/fleet/internal/lease"
	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/scheduler"
	"rlworld/fleet/internal/vmm"
)

var (
	// ErrUnknownLease means no active lease has the id.
	ErrUnknownLease = errors.New("unknown lease")
	// ErrInputFrozen means finish already froze the env; only close is allowed now.
	ErrInputFrozen = errors.New("env input is frozen")
	// ErrUnknownHost means a placement named a host without a node agent.
	ErrUnknownHost = errors.New("no node agent for host")
)

// Node is what fleetd needs from a node agent, in process or remote.
type Node interface {
	Start(ctx context.Context, request nodeagent.StartRequest) (nodeagent.EnvInfo, error)
	Destroy(ctx context.Context, envID string) error
	Step(ctx context.Context, envID string, action json.RawMessage) (nodeagent.StepResult, error)
	Observe(ctx context.Context, envID string) (nodeagent.StepResult, error)
	Fork(ctx context.Context, envID string, count int, checkpointDir string) ([]nodeagent.EnvInfo, error)
	Checkpoint(ctx context.Context, envID, statePath, memoryPath string) (vmm.SnapshotRef, error)
	Extract(ctx context.Context, envID string, apps []string) (json.RawMessage, error)
	Inject(ctx context.Context, envID string, payload json.RawMessage) error
	Prefetch(ctx context.Context, digest string) error
	CachedSnapshots() ([]string, error)
	BuildSnapshot(ctx context.Context, request nodeagent.BuildRequest) (nodeagent.BuildResult, error)
	Pause(ctx context.Context, envID string) error
	Resume(ctx context.Context, envID string) error
	DialVNC(ctx context.Context, envID string) (net.Conn, *bufio.Reader, error)
}

// Snapshot is the start state a lease group restores from.
type Snapshot struct {
	Digest       string `json:"digest"`
	StatePath    string `json:"state_path"`
	MemoryPath   string `json:"memory_path"`
	BaseDiskPath string `json:"base_disk_path"`
}

// GroupRequest asks for GroupSize envs of one snapshot, ideally on one host.
type GroupRequest struct {
	RunID              string
	Tenant             string
	CostCenter         string
	Pool               string
	Priority           admission.Priority
	Endpoint           string
	Snapshot           Snapshot
	PerVM              scheduler.Resources
	VCPUs              int
	GroupSize          int
	TTL                time.Duration
	HostScopedLicenses []string
	// WorldTime is the world's fixed clock that every env of the group starts at; zero means the host clock.
	WorldTime time.Time
	// ObservationKinds are what the task observes.
	ObservationKinds []string
}

// LeaseInfo is what a rollout worker gets back for each env; TakenOver means an operator drives it right now.
type LeaseInfo struct {
	LeaseID   string    `json:"lease_id"`
	EnvID     string    `json:"env_id"`
	HostID    string    `json:"host_id"`
	ExpiresAt time.Time `json:"expires_at"`
	TakenOver bool      `json:"taken_over"`
}

type binding struct {
	envID    string
	hostID   string
	request  GroupRequest
	frozen   bool
	snapshot Snapshot
	takeover *takeoverSession
}

// Service ties admission, leases, placement and node agents together.
type Service struct {
	mu            sync.Mutex
	leases        *lease.Manager
	scheduler     *scheduler.Scheduler
	nodes         map[string]Node
	bindings      map[string]*binding
	takeovers     map[string]*takeoverSession
	checkpointDir string
	nextEnvNumber int
	now           func() time.Time
	snapshots     *SnapshotSettings
	prefetches    sync.WaitGroup
}

// NewService wires lease endings to env teardown and capacity release.
func NewService(leases *lease.Manager, placement *scheduler.Scheduler, nodes map[string]Node, checkpointDir string) *Service {
	service := &Service{leases: leases, scheduler: placement, nodes: nodes, bindings: map[string]*binding{},
		takeovers: map[string]*takeoverSession{}, checkpointDir: checkpointDir, now: time.Now}
	leases.OnRelease(service.onLeaseEnded)
	return service
}

func (service *Service) onLeaseEnded(ended lease.Lease) {
	service.mu.Lock()
	bound, found := service.bindings[ended.ID]
	delete(service.bindings, ended.ID)
	service.mu.Unlock()
	if !found {
		return
	}
	service.mu.Lock()
	session := bound.takeover
	service.mu.Unlock()
	if session != nil {
		service.dropTakeover(bound, session)
	}
	if node, known := service.nodes[bound.hostID]; known {
		node.Destroy(context.Background(), bound.envID)
	}
	service.scheduler.Free(bound.hostID, bound.request.PerVM)
}

func (service *Service) newEnvID() string {
	service.mu.Lock()
	defer service.mu.Unlock()
	service.nextEnvNumber++
	return fmt.Sprintf("env_%08d", service.nextEnvNumber)
}

func leaseRequestFor(request GroupRequest) lease.Request {
	return lease.Request{RunID: request.RunID, Tenant: request.Tenant, CostCenter: request.CostCenter,
		Pool: request.Pool, Priority: request.Priority, Endpoint: request.Endpoint, TTL: request.TTL}
}

func (service *Service) lookup(leaseID string) (*binding, error) {
	service.mu.Lock()
	defer service.mu.Unlock()
	bound, found := service.bindings[leaseID]
	if !found {
		return nil, ErrUnknownLease
	}
	return bound, nil
}

func (service *Service) nodeFor(bound *binding) (Node, error) {
	node, found := service.nodes[bound.hostID]
	if !found {
		return nil, fmt.Errorf("%w: %s", ErrUnknownHost, bound.hostID)
	}
	return node, nil
}

// LeaseGroup places, admits and starts a group of envs, all or nothing.
func (service *Service) LeaseGroup(ctx context.Context, request GroupRequest) ([]LeaseInfo, error) {
	assignments, err := service.scheduler.Place(scheduler.Request{SnapshotDigest: request.Snapshot.Digest,
		Pool: request.Pool, PerVM: request.PerVM, Count: request.GroupSize, HostScopedLicenses: request.HostScopedLicenses})
	if err != nil {
		return nil, err
	}
	var hostIDs []string
	for _, assignment := range assignments {
		for range assignment.Count {
			hostIDs = append(hostIDs, assignment.HostID)
		}
	}
	return service.admitAndStart(ctx, request, hostIDs)
}

func (service *Service) admitAndStart(ctx context.Context, request GroupRequest, hostIDs []string) ([]LeaseInfo, error) {
	var admitted []lease.Lease
	for index, hostID := range hostIDs {
		created, err := service.leases.Admit(leaseRequestFor(request), hostID)
		if err != nil {
			service.abandon(admitted, hostIDs[index:], request.PerVM)
			return nil, err
		}
		admitted = append(admitted, created)
	}
	var infos []LeaseInfo
	for _, created := range admitted {
		envID := service.newEnvID()
		node, found := service.nodes[created.HostID]
		if !found {
			service.abandon(admitted, nil, request.PerVM)
			return nil, fmt.Errorf("%w: %s", ErrUnknownHost, created.HostID)
		}
		service.mu.Lock()
		service.bindings[created.ID] = &binding{envID: envID, hostID: created.HostID, request: request,
			snapshot: request.Snapshot}
		service.mu.Unlock()
		if _, err := node.Start(ctx, startRequest(envID, request, request.Snapshot)); err != nil {
			service.abandon(admitted, nil, request.PerVM)
			return nil, err
		}
		if request.Snapshot.Digest != "" {
			service.scheduler.MarkSnapshotCached(created.HostID, request.Snapshot.Digest)
		}
		infos = append(infos, LeaseInfo{LeaseID: created.ID, EnvID: envID, HostID: created.HostID,
			ExpiresAt: created.ExpiresAt})
	}
	return infos, nil
}

// abandon releases admitted leases (their hooks free capacity) and frees capacity that never got a lease.
func (service *Service) abandon(admitted []lease.Lease, unadmittedHostIDs []string, perVM scheduler.Resources) {
	for _, created := range admitted {
		service.mu.Lock()
		_, bound := service.bindings[created.ID]
		service.mu.Unlock()
		service.leases.Release(created.ID)
		if !bound {
			service.scheduler.Free(created.HostID, perVM)
		}
	}
	for _, hostID := range unadmittedHostIDs {
		service.scheduler.Free(hostID, perVM)
	}
}

func startRequest(envID string, request GroupRequest, snapshot Snapshot) nodeagent.StartRequest {
	return nodeagent.StartRequest{EnvID: envID, SnapshotDigest: snapshot.Digest, StatePath: snapshot.StatePath,
		MemoryPath:   snapshot.MemoryPath,
		BaseDiskPath: snapshot.BaseDiskPath, MemoryMiB: request.PerVM.MemoryMiB, VCPUs: request.VCPUs,
		WorldTime: request.WorldTime, ObservationKinds: request.ObservationKinds}
}

// Heartbeat keeps a lease alive.
func (service *Service) Heartbeat(leaseID string) (LeaseInfo, error) {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return LeaseInfo{}, err
	}
	renewed, err := service.leases.Heartbeat(leaseID)
	if err != nil {
		return LeaseInfo{}, ErrUnknownLease
	}
	return LeaseInfo{LeaseID: leaseID, EnvID: bound.envID, HostID: bound.hostID, ExpiresAt: renewed.ExpiresAt,
		TakenOver: service.takenOver(bound)}, nil
}

// Reset restores the lease's start snapshot into a fresh env on the same host and observes it.
func (service *Service) Reset(ctx context.Context, leaseID string) (nodeagent.StepResult, error) {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return nodeagent.StepResult{}, err
	}
	if service.takenOver(bound) {
		return nodeagent.StepResult{}, ErrTakenOver
	}
	node, err := service.nodeFor(bound)
	if err != nil {
		return nodeagent.StepResult{}, err
	}
	node.Destroy(ctx, bound.envID)
	envID := service.newEnvID()
	service.mu.Lock()
	bound.envID, bound.frozen = envID, false
	service.mu.Unlock()
	if _, err := node.Start(ctx, startRequest(envID, bound.request, bound.snapshot)); err != nil {
		return nodeagent.StepResult{}, err
	}
	return node.Observe(ctx, envID)
}

// Step runs one turn unless finish already froze the env.
func (service *Service) Step(ctx context.Context, leaseID string, action json.RawMessage) (nodeagent.StepResult, error) {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return nodeagent.StepResult{}, err
	}
	if bound.frozen {
		return nodeagent.StepResult{}, ErrInputFrozen
	}
	if service.takenOver(bound) {
		return nodeagent.StepResult{}, ErrTakenOver
	}
	node, err := service.nodeFor(bound)
	if err != nil {
		return nodeagent.StepResult{}, err
	}
	return node.Step(ctx, bound.envID, action)
}

// Fork checkpoints the lease's env and leases count clones on the same host.
func (service *Service) Fork(ctx context.Context, leaseID string, count int) ([]LeaseInfo, error) {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return nil, err
	}
	if service.takenOver(bound) {
		return nil, ErrTakenOver
	}
	node, err := service.nodeFor(bound)
	if err != nil {
		return nil, err
	}
	if err := service.scheduler.ReserveOnHost(bound.hostID, bound.request.PerVM, count); err != nil {
		return nil, err
	}
	var admitted []lease.Lease
	for range count {
		created, err := service.leases.Admit(leaseRequestFor(bound.request), bound.hostID)
		if err != nil {
			hostIDs := make([]string, count-len(admitted))
			for index := range hostIDs {
				hostIDs[index] = bound.hostID
			}
			service.abandon(admitted, hostIDs, bound.request.PerVM)
			return nil, err
		}
		admitted = append(admitted, created)
	}
	forks, err := node.Fork(ctx, bound.envID, count, service.checkpointDir)
	if err != nil {
		service.abandon(admitted, nil, bound.request.PerVM)
		return nil, err
	}
	var infos []LeaseInfo
	for index, created := range admitted {
		service.mu.Lock()
		service.bindings[created.ID] = &binding{envID: forks[index].EnvID, hostID: bound.hostID, request: bound.request,
			snapshot: bound.snapshot}
		service.mu.Unlock()
		infos = append(infos, LeaseInfo{LeaseID: created.ID, EnvID: forks[index].EnvID, HostID: bound.hostID,
			ExpiresAt: created.ExpiresAt})
	}
	return infos, nil
}

// Checkpoint writes a full snapshot of the lease's env.
func (service *Service) Checkpoint(ctx context.Context, leaseID string) (vmm.SnapshotRef, error) {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return vmm.SnapshotRef{}, err
	}
	if service.takenOver(bound) {
		return vmm.SnapshotRef{}, ErrTakenOver
	}
	node, err := service.nodeFor(bound)
	if err != nil {
		return vmm.SnapshotRef{}, err
	}
	prefix := filepath.Join(service.checkpointDir, bound.envID+"_checkpoint")
	return node.Checkpoint(ctx, bound.envID, prefix+".state", prefix+".memory")
}

// Finish freezes input first, then extracts app state for the reward pipeline.
func (service *Service) Finish(ctx context.Context, leaseID string, apps []string) (json.RawMessage, error) {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return nil, err
	}
	if service.takenOver(bound) {
		return nil, ErrTakenOver
	}
	node, err := service.nodeFor(bound)
	if err != nil {
		return nil, err
	}
	service.mu.Lock()
	bound.frozen = true
	service.mu.Unlock()
	if err := node.Pause(ctx, bound.envID); err != nil {
		return nil, err
	}
	return node.Extract(ctx, bound.envID, apps)
}

// Inject hands entitlements to the guest.
func (service *Service) Inject(ctx context.Context, leaseID string, payload json.RawMessage) error {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return err
	}
	if service.takenOver(bound) {
		return ErrTakenOver
	}
	node, err := service.nodeFor(bound)
	if err != nil {
		return err
	}
	return node.Inject(ctx, bound.envID, payload)
}

// Close ends the lease; the release hook destroys the env and frees capacity.
func (service *Service) Close(leaseID string) error {
	if _, err := service.lookup(leaseID); err != nil {
		return err
	}
	if _, err := service.leases.Release(leaseID); err != nil {
		return ErrUnknownLease
	}
	return nil
}

// AdmissionStatus reports fleet capacity, reservations, tenant quotas and endpoint budgets with their usage.
func (service *Service) AdmissionStatus() admission.Status {
	return service.leases.Admission().Status()
}

// SetTenantQuota changes a tenant's concurrent env quota.
func (service *Service) SetTenantQuota(tenant string, quota int) error {
	return service.leases.Admission().SetTenantQuota(tenant, quota)
}

// Leases lists active leases, optionally for one run.
func (service *Service) Leases(runID string) []LeaseInfo {
	var infos []LeaseInfo
	for _, active := range service.leases.Active(runID) {
		service.mu.Lock()
		bound, found := service.bindings[active.ID]
		service.mu.Unlock()
		if found {
			infos = append(infos, LeaseInfo{LeaseID: active.ID, EnvID: bound.envID, HostID: bound.hostID,
				ExpiresAt: active.ExpiresAt, TakenOver: service.takenOver(bound)})
		}
	}
	return infos
}
