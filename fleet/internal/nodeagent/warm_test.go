package nodeagent

import (
	"context"
	"errors"
	"slices"
	"testing"
	"time"

	"rlworld/fleet/internal/vmm"
)

func hotRequest(envID string) StartRequest {
	request := startRequest(envID)
	request.SnapshotDigest = "sha256:hot"
	return request
}

func TestWarmEnvsWaitPausedAndLeasesTakeThemInsteadOfRestoring(t *testing.T) {
	ctx := context.Background()
	agent, driver, _, _ := newTestAgent(&scriptedGuest{hashes: []string{"a"}})
	worldTime := time.Date(2026, 3, 14, 9, 0, 0, 0, time.UTC)
	template := hotRequest("ignored")
	template.WorldTime, template.Resume = worldTime, true
	first, err := agent.Warm(ctx, template)
	if err != nil || first.EnvID != "warm_000001" || !first.Warm || first.State != vmm.Paused {
		t.Fatalf("warm: %+v %v", first, err)
	}
	if driver.Restores[0].Resume || driver.Restores[0].VMID != "warm_000001" {
		t.Fatalf("warm envs restore paused: %+v", driver.Restores[0])
	}
	if !agent.envs["warm_000001"].request.WorldTime.IsZero() {
		t.Fatal("a warm env keeps no world clock until a lease takes it")
	}
	if second, _ := agent.Warm(ctx, template); second.EnvID != "warm_000002" || len(driver.Restores) != 2 {
		t.Fatalf("warming does not take an env that is already warm: %+v", second)
	}
	if counts := agent.WarmEnvs(); counts["sha256:hot"] != 2 || len(counts) != 1 {
		t.Fatalf("warm counts: %v", counts)
	}
	lease := hotRequest("env_9")
	lease.WorldTime, lease.ObservationKinds, lease.Resume = worldTime, []string{"screenshot"}, true
	taken, err := agent.Start(ctx, lease)
	if err != nil || taken.EnvID != "warm_000001" || !taken.Warm || taken.State != vmm.Running {
		t.Fatalf("the lease takes the oldest warm env: %+v %v", taken, err)
	}
	if len(driver.Restores) != 2 || agent.WarmEnvs()["sha256:hot"] != 1 {
		t.Fatalf("no restore happens for a warm hand over: %d restores, %v", len(driver.Restores), agent.WarmEnvs())
	}
	handed := agent.envs["warm_000001"].request
	if !handed.WorldTime.Equal(worldTime) || !slices.Equal(handed.ObservationKinds, []string{"screenshot"}) {
		t.Fatalf("the lease's clock and observations apply: %+v", handed)
	}
	cold := startRequest("env_10")
	cold.SnapshotDigest = "sha256:cold"
	if started, err := agent.Start(ctx, cold); err != nil || started.Warm || len(driver.Restores) != 3 {
		t.Fatalf("another snapshot restores as usual: %+v %v", started, err)
	}
	for _, info := range agent.Envs() {
		if info.EnvID == "warm_000002" && !info.Warm {
			t.Fatalf("the waiting env lists as warm: %+v", info)
		}
	}
	cooled, err := agent.Cool(ctx, "sha256:hot", 5)
	if err != nil || cooled != 1 || len(agent.WarmEnvs()) != 0 {
		t.Fatalf("cooling destroys what waits: %d %v %v", cooled, err, agent.WarmEnvs())
	}
	if !slices.Contains(driver.Operations, "destroy warm_000002") {
		t.Fatalf("operations %v", driver.Operations)
	}
	if _, err := agent.Warm(ctx, startRequest("x")); !errors.Is(err, ErrNoWarmSnapshot) {
		t.Fatalf("warm envs need a digest: %v", err)
	}
	if cooled, err := agent.Cool(ctx, "sha256:missing", 1); cooled != 0 || err != nil {
		t.Fatalf("cooling nothing: %d %v", cooled, err)
	}
}

func TestAWarmEnvThatCannotResumeIsDestroyed(t *testing.T) {
	ctx := context.Background()
	agent, driver, _, _ := newTestAgent(&scriptedGuest{hashes: []string{"a"}})
	if _, err := agent.Warm(ctx, hotRequest("ignored")); err != nil {
		t.Fatal(err)
	}
	driver.FailNext["resume"] = errors.New("vcpu thread died")
	lease := hotRequest("env_1")
	lease.Resume = true
	if _, err := agent.Start(ctx, lease); err == nil {
		t.Fatal("a failed resume fails the lease")
	}
	if len(agent.Envs()) != 0 || driver.State("warm_000001") != vmm.Destroyed {
		t.Fatalf("the broken env is gone: %v", agent.Envs())
	}
	driver.FailNext["restore"] = errors.New("snapshot unreadable")
	if _, err := agent.Warm(ctx, hotRequest("ignored")); err == nil || len(agent.WarmEnvs()) != 0 {
		t.Fatalf("a failed warm restore leaves nothing waiting: %v", err)
	}
}
