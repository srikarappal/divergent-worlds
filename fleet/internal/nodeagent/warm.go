package nodeagent

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"time"

	"rlworld/fleet/internal/vmm"
)

// ErrNoWarmSnapshot means a warm env was asked for without naming its snapshot by digest.
var ErrNoWarmSnapshot = errors.New("warm envs need a snapshot digest")

// Warm restores one env of a snapshot ahead of demand and leaves it paused, so a lease takes it without waiting for
// a restore (DESIGN.md 6.7.10: warm pools of restored, paused VMs for hot snapshots, mostly for Windows). The
// world clock and observations are the lease's, set when it takes the env.
func (agent *Agent) Warm(ctx context.Context, template StartRequest) (EnvInfo, error) {
	if template.SnapshotDigest == "" {
		return EnvInfo{}, ErrNoWarmSnapshot
	}
	agent.mu.Lock()
	agent.nextWarmNumber++
	envID := fmt.Sprintf("warm_%06d", agent.nextWarmNumber)
	agent.mu.Unlock()
	template.EnvID, template.Resume, template.GuestElapsed = envID, false, 0
	template.WorldTime, template.ObservationKinds = time.Time{}, nil
	info, err := agent.restore(ctx, template)
	if err != nil {
		return EnvInfo{}, err
	}
	agent.mu.Lock()
	if restored, found := agent.envs[envID]; found {
		restored.warm = true
	}
	agent.mu.Unlock()
	info.Warm = true
	return info, nil
}

// takeWarm claims the oldest warm env of a snapshot so no other lease or cooling takes it too.
func (agent *Agent) takeWarm(digest string) string {
	if digest == "" {
		return ""
	}
	agent.mu.Lock()
	defer agent.mu.Unlock()
	var waiting []string
	for envID, current := range agent.envs {
		if current.warm && current.request.SnapshotDigest == digest {
			waiting = append(waiting, envID)
		}
	}
	if len(waiting) == 0 {
		return ""
	}
	slices.Sort(waiting)
	agent.envs[waiting[0]].warm = false
	return waiting[0]
}

// handOver gives a warm env to a lease: the lease's world clock and observations, resumed if it asked.
func (agent *Agent) handOver(ctx context.Context, envID string, request StartRequest) (EnvInfo, error) {
	agent.mu.Lock()
	taken := agent.envs[envID]
	taken.request.WorldTime, taken.request.ObservationKinds = request.WorldTime, request.ObservationKinds
	info := EnvInfo{EnvID: envID, State: taken.state, Namespace: taken.network.Namespace,
		CloneAddress: cloneAddress(taken.network), Warm: true}
	agent.mu.Unlock()
	if request.Resume {
		if err := agent.Resume(ctx, envID); err != nil {
			return EnvInfo{}, errors.Join(err, agent.Destroy(ctx, envID))
		}
		info.State = vmm.Running
	}
	return info, nil
}

// WarmEnvs counts the warm envs waiting per snapshot digest.
func (agent *Agent) WarmEnvs() map[string]int {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	counts := map[string]int{}
	for _, current := range agent.envs {
		if current.warm {
			counts[current.request.SnapshotDigest]++
		}
	}
	return counts
}

// Cool destroys up to count warm envs of a snapshot that went cold and reports how many went.
func (agent *Agent) Cool(ctx context.Context, digest string, count int) (int, error) {
	var problems []error
	cooled := 0
	for cooled < count {
		envID := agent.takeWarm(digest)
		if envID == "" {
			break
		}
		problems = append(problems, agent.Destroy(ctx, envID))
		cooled++
	}
	return cooled, errors.Join(problems...)
}
