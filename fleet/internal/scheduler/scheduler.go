// Package scheduler places envs on hosts (DESIGN.md sections 6.1 and 6.7).
//
// Placement filters by OS pool, free CPU, memory and GPU and host scoped
// license seats, then ranks hosts: a warm paused VM of the snapshot beats a
// cached snapshot, which beats a fetch; a host already holding a host scoped
// license beats one that would take a new seat. A GRPO group stays on one
// host when it fits, because forks of one snapshot share memory pages there.
package scheduler

import (
	"errors"
	"fmt"
	"sort"
	"sync"
)

// ErrNoCapacity means no set of hosts can take the request.
var ErrNoCapacity = errors.New("no host capacity for this request")

const (
	warmVMScore          = 1000
	cachedSnapshotScore  = 100
	heldLicenseScore     = 10
	fullHostTiebreakUnit = 1
)

// Resources is what one VM needs or what a host has free.
type Resources struct {
	CPUMillicores int
	MemoryMiB     int
	GPUs          int
}

func (available Resources) fits(needed Resources, count int) bool {
	return available.CPUMillicores >= needed.CPUMillicores*count &&
		available.MemoryMiB >= needed.MemoryMiB*count &&
		available.GPUs >= needed.GPUs*count
}

func (available Resources) maxFit(needed Resources) int {
	fit := int(^uint(0) >> 1)
	for _, pair := range [][2]int{
		{available.CPUMillicores, needed.CPUMillicores},
		{available.MemoryMiB, needed.MemoryMiB},
		{available.GPUs, needed.GPUs},
	} {
		if pair[1] > 0 && pair[0]/pair[1] < fit {
			fit = pair[0] / pair[1]
		}
	}
	return fit
}

func (available Resources) minus(needed Resources, count int) Resources {
	return Resources{
		CPUMillicores: available.CPUMillicores - needed.CPUMillicores*count,
		MemoryMiB:     available.MemoryMiB - needed.MemoryMiB*count,
		GPUs:          available.GPUs - needed.GPUs*count,
	}
}

func (available Resources) plus(returned Resources, count int) Resources {
	return available.minus(returned, -count)
}

// Host is one bare metal machine in one VMM pool.
type Host struct {
	ID              string
	Pool            string
	Capacity        Resources
	Draining        bool
	CachedSnapshots map[string]bool
	WarmVMs         map[string]int
	HeldLicenses    map[string]bool
}

// Request asks for Count VMs of one snapshot.
type Request struct {
	SnapshotDigest      string
	Pool                string
	PerVM               Resources
	Count               int
	HostScopedLicenses  []string
	FreeHostLicenseSeat map[string]int
}

// Assignment places some of a request's VMs on one host.
type Assignment struct {
	HostID      string
	Count       int
	WarmVMsUsed int
	NeedsFetch  bool
}

type hostState struct {
	host      Host
	allocated Resources
}

func (state *hostState) free() Resources {
	return state.host.Capacity.minus(state.allocated, 1)
}

// Scheduler holds hosts and their allocations.
type Scheduler struct {
	mu    sync.Mutex
	hosts map[string]*hostState
}

// New returns an empty scheduler.
func New() *Scheduler {
	return &Scheduler{hosts: map[string]*hostState{}}
}

// UpsertHost adds a host or replaces its description while keeping its allocations.
func (scheduler *Scheduler) UpsertHost(host Host) {
	scheduler.mu.Lock()
	defer scheduler.mu.Unlock()
	if host.CachedSnapshots == nil {
		host.CachedSnapshots = map[string]bool{}
	}
	if host.WarmVMs == nil {
		host.WarmVMs = map[string]int{}
	}
	if host.HeldLicenses == nil {
		host.HeldLicenses = map[string]bool{}
	}
	if existing, found := scheduler.hosts[host.ID]; found {
		existing.host = host
		return
	}
	scheduler.hosts[host.ID] = &hostState{host: host}
}

// MarkSnapshotCached records that a host now has a snapshot on local NVMe.
func (scheduler *Scheduler) MarkSnapshotCached(hostID, snapshotDigest string) {
	scheduler.mu.Lock()
	defer scheduler.mu.Unlock()
	if state, found := scheduler.hosts[hostID]; found {
		state.host.CachedSnapshots[snapshotDigest] = true
	}
}

// SetWarmVMs records how many restored, paused VMs of a snapshot wait on a host.
func (scheduler *Scheduler) SetWarmVMs(hostID, snapshotDigest string, count int) {
	scheduler.mu.Lock()
	defer scheduler.mu.Unlock()
	if state, found := scheduler.hosts[hostID]; found {
		state.host.WarmVMs[snapshotDigest] = count
	}
}

func (scheduler *Scheduler) score(state *hostState, request Request) int {
	score := 0
	if state.host.WarmVMs[request.SnapshotDigest] > 0 {
		score += warmVMScore
	}
	if state.host.CachedSnapshots[request.SnapshotDigest] {
		score += cachedSnapshotScore
	}
	for _, license := range request.HostScopedLicenses {
		if state.host.HeldLicenses[license] {
			score += heldLicenseScore
		}
	}
	return score
}

func (scheduler *Scheduler) eligible(state *hostState, request Request, seatsLeft map[string]int) bool {
	if state.host.Pool != request.Pool || state.host.Draining || !state.free().fits(request.PerVM, 1) {
		return false
	}
	for _, license := range request.HostScopedLicenses {
		if !state.host.HeldLicenses[license] && seatsLeft[license] <= 0 {
			return false
		}
	}
	return true
}

// rankedHosts orders eligible hosts best first; ties go to the fuller host to keep others free, then by id.
func (scheduler *Scheduler) rankedHosts(request Request, seatsLeft map[string]int) []*hostState {
	var candidates []*hostState
	for _, state := range scheduler.hosts {
		if scheduler.eligible(state, request, seatsLeft) {
			candidates = append(candidates, state)
		}
	}
	sort.Slice(candidates, func(first, second int) bool {
		firstScore, secondScore := scheduler.score(candidates[first], request), scheduler.score(candidates[second], request)
		if firstScore != secondScore {
			return firstScore > secondScore
		}
		firstFree, secondFree := candidates[first].free().MemoryMiB, candidates[second].free().MemoryMiB
		if firstFree != secondFree {
			return firstFree < secondFree
		}
		return candidates[first].host.ID < candidates[second].host.ID
	})
	return candidates
}

// Place reserves resources for all of the request's VMs, or for none of them.
func (scheduler *Scheduler) Place(request Request) ([]Assignment, error) {
	if request.Count < 1 {
		return nil, fmt.Errorf("request count must be positive, got %d", request.Count)
	}
	scheduler.mu.Lock()
	defer scheduler.mu.Unlock()
	seatsLeft := map[string]int{}
	for license, free := range request.FreeHostLicenseSeat {
		seatsLeft[license] = free
	}
	ranked := scheduler.rankedHosts(request, seatsLeft)
	for _, state := range ranked {
		if state.free().fits(request.PerVM, request.Count) {
			return []Assignment{scheduler.assign(state, request, request.Count, seatsLeft)}, nil
		}
	}
	var assignments []Assignment
	remaining := request.Count
	for _, state := range ranked {
		if remaining == 0 {
			break
		}
		if !scheduler.eligible(state, request, seatsLeft) {
			continue
		}
		placed := min(remaining, state.free().maxFit(request.PerVM))
		assignments = append(assignments, scheduler.assign(state, request, placed, seatsLeft))
		remaining -= placed
	}
	if remaining > 0 {
		for _, assignment := range assignments {
			scheduler.undo(assignment, request)
		}
		return nil, fmt.Errorf("%w: %d of %d VMs did not fit in pool %s", ErrNoCapacity, remaining, request.Count,
			request.Pool)
	}
	return assignments, nil
}

func (scheduler *Scheduler) assign(state *hostState, request Request, count int, seatsLeft map[string]int) Assignment {
	state.allocated = state.allocated.plus(request.PerVM, count)
	for _, license := range request.HostScopedLicenses {
		if !state.host.HeldLicenses[license] {
			state.host.HeldLicenses[license] = true
			seatsLeft[license]--
		}
	}
	warmUsed := min(count, state.host.WarmVMs[request.SnapshotDigest])
	state.host.WarmVMs[request.SnapshotDigest] -= warmUsed
	return Assignment{
		HostID:      state.host.ID,
		Count:       count,
		WarmVMsUsed: warmUsed,
		NeedsFetch:  !state.host.CachedSnapshots[request.SnapshotDigest],
	}
}

func (scheduler *Scheduler) undo(assignment Assignment, request Request) {
	state := scheduler.hosts[assignment.HostID]
	state.allocated = state.allocated.minus(request.PerVM, assignment.Count)
	state.host.WarmVMs[request.SnapshotDigest] += assignment.WarmVMsUsed
}

// Free returns one finished VM's resources to its host.
func (scheduler *Scheduler) Free(hostID string, perVM Resources) {
	scheduler.mu.Lock()
	defer scheduler.mu.Unlock()
	if state, found := scheduler.hosts[hostID]; found {
		state.allocated = state.allocated.minus(perVM, 1)
	}
}

// ReleaseHostLicense records that the last env needing a host scoped license left the host.
func (scheduler *Scheduler) ReleaseHostLicense(hostID, license string) {
	scheduler.mu.Lock()
	defer scheduler.mu.Unlock()
	if state, found := scheduler.hosts[hostID]; found {
		delete(state.host.HeldLicenses, license)
	}
}

// FreeResources reports a host's unallocated resources.
func (scheduler *Scheduler) FreeResources(hostID string) Resources {
	scheduler.mu.Lock()
	defer scheduler.mu.Unlock()
	if state, found := scheduler.hosts[hostID]; found {
		return state.free()
	}
	return Resources{}
}

// PlanPrefetch picks up to hostsWanted pool hosts without the snapshot, those with the most free memory first.
func (scheduler *Scheduler) PlanPrefetch(snapshotDigest, pool string, hostsWanted int) []string {
	scheduler.mu.Lock()
	defer scheduler.mu.Unlock()
	var candidates []*hostState
	for _, state := range scheduler.hosts {
		if state.host.Pool == pool && !state.host.Draining && !state.host.CachedSnapshots[snapshotDigest] {
			candidates = append(candidates, state)
		}
	}
	sort.Slice(candidates, func(first, second int) bool {
		firstFree, secondFree := candidates[first].free().MemoryMiB, candidates[second].free().MemoryMiB
		if firstFree != secondFree {
			return firstFree > secondFree
		}
		return candidates[first].host.ID < candidates[second].host.ID
	})
	var hostIDs []string
	for _, state := range candidates[:min(hostsWanted, len(candidates))] {
		hostIDs = append(hostIDs, state.host.ID)
	}
	return hostIDs
}
