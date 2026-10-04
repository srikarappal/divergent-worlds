package fleetd

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestHotSnapshotsKeepWarmEnvsThatLeasesTakeWithoutARestore(t *testing.T) {
	ctx := context.Background()
	fleet := newTestFleet(t, 16, 16_384)
	fleet.service.now = fleet.clock.Now
	fleet.service.EnableWarmPools(WarmPolicy{PerSnapshot: 2, IdleAfter: 10 * time.Minute})
	if err := fleet.service.MaintainWarmPools(ctx); err != nil || len(fleet.driver.Restores) != 0 {
		t.Fatalf("nothing is hot before a lease: %v %d", err, len(fleet.driver.Restores))
	}
	status, first := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	if status != http.StatusCreated {
		t.Fatalf("lease: %d %v", status, first)
	}
	if err := fleet.service.MaintainWarmPools(ctx); err != nil {
		t.Fatal(err)
	}
	if len(fleet.driver.Restores) != 3 {
		t.Fatalf("the hot snapshot gets two warm envs beside the leased one: %d restores", len(fleet.driver.Restores))
	}
	if free := fleet.scheduler.FreeResources("host_a"); free.MemoryMiB != 16_384-3*2048 {
		t.Fatalf("warm envs hold their memory: %+v", free)
	}
	status, second := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	envID := second["leases"].([]any)[0].(map[string]any)["env_id"].(string)
	if status != http.StatusCreated || !strings.HasPrefix(envID, "warm_") || len(fleet.driver.Restores) != 3 {
		t.Fatalf("the second lease takes a warm env without restoring: %d %s %d", status, envID,
			len(fleet.driver.Restores))
	}
	if free := fleet.scheduler.FreeResources("host_a"); free.MemoryMiB != 16_384-3*2048 {
		t.Fatalf("the warm env's reservation becomes the lease's: %+v", free)
	}
	leaseID := second["leases"].([]any)[0].(map[string]any)["lease_id"].(string)
	if status, stepped := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/step",
		map[string]any{"action": map[string]any{"name": "wait", "duration_ms": 10}}); status != http.StatusOK {
		t.Fatalf("the warm env steps under its lease: %d %v", status, stepped)
	}
	if err := fleet.service.MaintainWarmPools(ctx); err != nil || len(fleet.driver.Restores) != 4 {
		t.Fatalf("the pool refills: %v %d", err, len(fleet.driver.Restores))
	}
	fleet.clock.Advance(11 * time.Minute)
	if err := fleet.service.MaintainWarmPools(ctx); err != nil {
		t.Fatal(err)
	}
	if free := fleet.scheduler.FreeResources("host_a"); free.MemoryMiB != 16_384-2*2048 {
		t.Fatalf("a cold snapshot's warm envs give their memory back: %+v", free)
	}
	if warm := fleet.service.nodes["host_a"].WarmEnvs(); len(warm) != 0 {
		t.Fatalf("no warm envs stay for a cold snapshot: %v", warm)
	}
	if err := fleet.service.MaintainWarmPools(ctx); err != nil || len(fleet.service.warm.templates) != 0 {
		t.Fatalf("a cold snapshot with nothing warm is forgotten: %v %v", err, fleet.service.warm.templates)
	}
}

func TestAFullHostKeepsASmallerWarmPool(t *testing.T) {
	ctx := context.Background()
	fleet := newTestFleet(t, 16, 3*2048)
	fleet.service.now = fleet.clock.Now
	fleet.service.EnableWarmPools(WarmPolicy{PerSnapshot: 4, IdleAfter: time.Hour})
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1)); status != http.StatusCreated {
		t.Fatalf("lease: %d", status)
	}
	if err := fleet.service.MaintainWarmPools(ctx); err != nil {
		t.Fatal(err)
	}
	if warm := fleet.service.nodes["host_a"].WarmEnvs()["sha256:snap"]; warm != 2 {
		t.Fatalf("only the room left becomes warm envs: %d", warm)
	}
	if free := fleet.scheduler.FreeResources("host_a"); free.MemoryMiB != 0 {
		t.Fatalf("the host is full: %+v", free)
	}
}

func TestWarmPoolsAreOffUnlessEnabled(t *testing.T) {
	fleet := newTestFleet(t, 16, 16_384)
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1)); status != http.StatusCreated {
		t.Fatalf("lease: %d", status)
	}
	if err := fleet.service.MaintainWarmPools(context.Background()); err != nil || len(fleet.driver.Restores) != 1 {
		t.Fatalf("no warm envs without a policy: %v %d", err, len(fleet.driver.Restores))
	}
}
