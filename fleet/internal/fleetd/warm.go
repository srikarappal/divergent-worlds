package fleetd

import (
	"context"
	"errors"
	"time"
)

// WarmPolicy keeps restored, paused envs of hot snapshots on each host (DESIGN.md 6.7.10), mostly for Windows,
// whose restores take seconds. A snapshot is hot on a host while leases keep starting there; once none has for
// IdleAfter its warm envs go, giving their memory back.
type WarmPolicy struct {
	PerSnapshot int
	IdleAfter   time.Duration
}

type warmKey struct {
	hostID string
	digest string
}

// warmPool remembers, per host and snapshot, when a lease last started and the request a warm env copies.
type warmPool struct {
	policy    WarmPolicy
	lastUsed  map[warmKey]time.Time
	templates map[warmKey]GroupRequest
}

// EnableWarmPools turns warm pools on.
func (service *Service) EnableWarmPools(policy WarmPolicy) {
	service.mu.Lock()
	defer service.mu.Unlock()
	service.warm = &warmPool{policy: policy, lastUsed: map[warmKey]time.Time{}, templates: map[warmKey]GroupRequest{}}
}

// noteLease keeps the lease's snapshot hot on its host. A lease that took a warm env frees that env's
// reservation, since placement reserved room for the lease itself.
func (service *Service) noteLease(hostID string, request GroupRequest, tookWarm bool) {
	service.mu.Lock()
	defer service.mu.Unlock()
	if service.warm == nil || request.Snapshot.Digest == "" {
		return
	}
	key := warmKey{hostID: hostID, digest: request.Snapshot.Digest}
	if template, found := service.warm.templates[key]; found && tookWarm {
		service.scheduler.Free(hostID, template.PerVM)
	}
	service.warm.lastUsed[key] = service.now()
	service.warm.templates[key] = request
}

// MaintainWarmPools tops up the warm envs of hot snapshots, removes those of cold ones and tells placement how
// many wait on each host. Warm envs hold their resources, so a full host keeps a smaller pool.
func (service *Service) MaintainWarmPools(ctx context.Context) error {
	service.mu.Lock()
	if service.warm == nil {
		service.mu.Unlock()
		return nil
	}
	policy := service.warm.policy
	templates := map[warmKey]GroupRequest{}
	hot := map[warmKey]bool{}
	for key, template := range service.warm.templates {
		templates[key] = template
		hot[key] = service.now().Sub(service.warm.lastUsed[key]) < policy.IdleAfter
	}
	service.mu.Unlock()
	var problems []error
	for key, template := range templates {
		node, found := service.nodes[key.hostID]
		if !found {
			continue
		}
		want := 0
		if hot[key] {
			want = policy.PerSnapshot
		}
		have, err := service.resizeWarm(ctx, node, key, template, want)
		problems = append(problems, err)
		service.scheduler.SetWarmVMs(key.hostID, key.digest, have)
		if want == 0 && have == 0 {
			service.mu.Lock()
			delete(service.warm.templates, key)
			delete(service.warm.lastUsed, key)
			service.mu.Unlock()
		}
	}
	return errors.Join(problems...)
}

// resizeWarm restores or destroys warm envs of one snapshot on one host until want wait there as far as room
// allows, then reports how many do.
func (service *Service) resizeWarm(ctx context.Context, node Node, key warmKey, template GroupRequest,
	want int) (int, error) {
	have := node.WarmEnvs()[key.digest]
	for have < want {
		if service.scheduler.ReserveOnHost(key.hostID, template.PerVM, 1) != nil {
			return have, nil
		}
		if _, err := node.Warm(ctx, startRequest("", template, template.Snapshot)); err != nil {
			service.scheduler.Free(key.hostID, template.PerVM)
			return have, err
		}
		have++
	}
	if have > want {
		cooled, err := node.Cool(ctx, key.digest, have-want)
		for range cooled {
			service.scheduler.Free(key.hostID, template.PerVM)
		}
		return have - cooled, err
	}
	return have, nil
}
