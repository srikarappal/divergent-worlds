// Package lease tracks env leases (DESIGN.md section 6.7).
//
// A lease carries a TTL that heartbeats extend. The reaper releases leases
// whose holder stopped heartbeating; release hooks destroy the VM and hand
// seats and identities back, so nothing leaks when a rollout worker dies.
// Every lease is tagged with run, tenant and cost center for billing.
package lease

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"

	"rlworld/fleet/internal/admission"
	"rlworld/fleet/internal/clock"
)

// State is where a lease is in its life.
type State string

const (
	// Active leases hold an env.
	Active State = "active"
	// Released leases ended normally.
	Released State = "released"
	// Expired leases were reaped after missing heartbeats.
	Expired State = "expired"
)

// ErrNotFound means no active lease has the given id.
var ErrNotFound = errors.New("no active lease with this id")

// Request asks for one env.
type Request struct {
	RunID      string
	Tenant     string
	CostCenter string
	Pool       string
	Priority   admission.Priority
	Endpoint   string
	TTL        time.Duration
}

// Lease is one env held by one rollout worker.
type Lease struct {
	ID        string
	Request   Request
	HostID    string
	State     State
	CreatedAt time.Time
	ExpiresAt time.Time
	EndedAt   time.Time
}

// ReleaseHook runs after a lease ends, outside the manager lock.
type ReleaseHook func(ended Lease)

// Manager admits, renews, releases and reaps leases.
type Manager struct {
	mu         sync.Mutex
	admission  *admission.Controller
	clock      clock.Clock
	defaultTTL time.Duration
	leases     map[string]Lease
	nextID     int
	hooks      []ReleaseHook
}

// NewManager returns a manager that admits through the given controller.
func NewManager(controller *admission.Controller, managerClock clock.Clock, defaultTTL time.Duration) *Manager {
	return &Manager{admission: controller, clock: managerClock, defaultTTL: defaultTTL, leases: map[string]Lease{}}
}

// Admission is the controller that admits this manager's leases.
func (manager *Manager) Admission() *admission.Controller {
	return manager.admission
}

// OnRelease registers a hook that runs for every lease that ends.
func (manager *Manager) OnRelease(hook ReleaseHook) {
	manager.mu.Lock()
	defer manager.mu.Unlock()
	manager.hooks = append(manager.hooks, hook)
}

func admissionRequest(request Request) admission.Request {
	return admission.Request{Tenant: request.Tenant, Priority: request.Priority, Endpoint: request.Endpoint}
}

// Admit creates an active lease on hostID if admission allows it.
func (manager *Manager) Admit(request Request, hostID string) (Lease, error) {
	if err := manager.admission.TryAdmit(admissionRequest(request)); err != nil {
		return Lease{}, err
	}
	if request.TTL <= 0 {
		request.TTL = manager.defaultTTL
	}
	manager.mu.Lock()
	defer manager.mu.Unlock()
	manager.nextID++
	now := manager.clock.Now()
	created := Lease{
		ID:        fmt.Sprintf("lease_%08d", manager.nextID),
		Request:   request,
		HostID:    hostID,
		State:     Active,
		CreatedAt: now,
		ExpiresAt: now.Add(request.TTL),
	}
	manager.leases[created.ID] = created
	return created, nil
}

// Heartbeat pushes an active lease's expiry out by its TTL.
func (manager *Manager) Heartbeat(leaseID string) (Lease, error) {
	manager.mu.Lock()
	defer manager.mu.Unlock()
	current, found := manager.leases[leaseID]
	if !found {
		return Lease{}, ErrNotFound
	}
	current.ExpiresAt = manager.clock.Now().Add(current.Request.TTL)
	manager.leases[leaseID] = current
	return current, nil
}

// Get returns an active lease.
func (manager *Manager) Get(leaseID string) (Lease, error) {
	manager.mu.Lock()
	defer manager.mu.Unlock()
	current, found := manager.leases[leaseID]
	if !found {
		return Lease{}, ErrNotFound
	}
	return current, nil
}

// Release ends an active lease normally.
func (manager *Manager) Release(leaseID string) (Lease, error) {
	return manager.end(leaseID, Released)
}

func (manager *Manager) end(leaseID string, endState State) (Lease, error) {
	manager.mu.Lock()
	current, found := manager.leases[leaseID]
	if !found {
		manager.mu.Unlock()
		return Lease{}, ErrNotFound
	}
	delete(manager.leases, leaseID)
	current.State = endState
	current.EndedAt = manager.clock.Now()
	hooks := append([]ReleaseHook(nil), manager.hooks...)
	manager.mu.Unlock()
	manager.admission.Release(admissionRequest(current.Request))
	for _, hook := range hooks {
		hook(current)
	}
	return current, nil
}

// ReapExpired ends every lease whose expiry has passed and returns them in id order.
func (manager *Manager) ReapExpired() []Lease {
	now := manager.clock.Now()
	manager.mu.Lock()
	var expiredIDs []string
	for leaseID, current := range manager.leases {
		if !current.ExpiresAt.After(now) {
			expiredIDs = append(expiredIDs, leaseID)
		}
	}
	manager.mu.Unlock()
	sort.Strings(expiredIDs)
	var reaped []Lease
	for _, leaseID := range expiredIDs {
		if ended, err := manager.end(leaseID, Expired); err == nil {
			reaped = append(reaped, ended)
		}
	}
	return reaped
}

// RunReaper reaps on every tick until the context ends.
func (manager *Manager) RunReaper(ctx context.Context, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			manager.ReapExpired()
		}
	}
}

// Active lists active leases in id order, optionally for one run.
func (manager *Manager) Active(runID string) []Lease {
	manager.mu.Lock()
	defer manager.mu.Unlock()
	var activeLeases []Lease
	for _, current := range manager.leases {
		if runID == "" || current.Request.RunID == runID {
			activeLeases = append(activeLeases, current)
		}
	}
	slices.SortFunc(activeLeases, compareLeaseIDs)
	return activeLeases
}

func compareLeaseIDs(first, second Lease) int {
	return strings.Compare(first.ID, second.ID)
}
