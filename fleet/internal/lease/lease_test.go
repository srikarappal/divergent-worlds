package lease

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/admission"
	"rlworld/fleet/internal/clock"
)

func newTestManager(t *testing.T, capacity int) (*Manager, *clock.Fake) {
	t.Helper()
	controller, err := admission.NewController(admission.Config{Capacity: capacity})
	if err != nil {
		t.Fatalf("controller: %v", err)
	}
	fakeClock := clock.NewFake(time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC))
	return NewManager(controller, fakeClock, time.Minute), fakeClock
}

func trainingRequest(runID string) Request {
	return Request{RunID: runID, Tenant: "lab", CostCenter: "research", Pool: "firecracker",
		Priority: admission.Training, Endpoint: "policy"}
}

func TestAdmitTagsAndExpiresLeases(t *testing.T) {
	manager, fakeClock := newTestManager(t, 4)
	admitted, err := manager.Admit(trainingRequest("run_1"), "host_a")
	if err != nil {
		t.Fatalf("admit: %v", err)
	}
	if admitted.ID != "lease_00000001" || admitted.State != Active || admitted.HostID != "host_a" {
		t.Fatalf("unexpected lease %+v", admitted)
	}
	if want := fakeClock.Now().Add(time.Minute); !admitted.ExpiresAt.Equal(want) {
		t.Fatalf("expiry: got %v, want %v", admitted.ExpiresAt, want)
	}
	if admitted.Request.CostCenter != "research" {
		t.Fatalf("cost center tag lost: %+v", admitted.Request)
	}
}

func TestHeartbeatsKeepLeasesAlive(t *testing.T) {
	manager, fakeClock := newTestManager(t, 4)
	kept, _ := manager.Admit(trainingRequest("run_1"), "host_a")
	dropped, _ := manager.Admit(trainingRequest("run_1"), "host_a")
	fakeClock.Advance(50 * time.Second)
	if _, err := manager.Heartbeat(kept.ID); err != nil {
		t.Fatalf("heartbeat: %v", err)
	}
	fakeClock.Advance(20 * time.Second)
	reaped := manager.ReapExpired()
	if len(reaped) != 1 || reaped[0].ID != dropped.ID || reaped[0].State != Expired {
		t.Fatalf("reaped %+v, want only %s", reaped, dropped.ID)
	}
	if _, err := manager.Get(kept.ID); err != nil {
		t.Fatalf("kept lease was reaped: %v", err)
	}
	if _, err := manager.Heartbeat(dropped.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("heartbeat on a reaped lease: got %v", err)
	}
}

// releaseRecorder keeps every lease its hook saw end.
type releaseRecorder struct {
	mu    sync.Mutex
	ended []Lease
}

func (recorder *releaseRecorder) record(endedLease Lease) {
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	recorder.ended = append(recorder.ended, endedLease)
}

func TestReleaseRunsHooksAndFreesCapacity(t *testing.T) {
	manager, _ := newTestManager(t, 1)
	recorder := &releaseRecorder{}
	manager.OnRelease(recorder.record)
	first, _ := manager.Admit(trainingRequest("run_1"), "host_a")
	if _, err := manager.Admit(trainingRequest("run_1"), "host_a"); !errors.Is(err, admission.ErrFleetFull) {
		t.Fatalf("second admit on a full fleet: got %v", err)
	}
	released, err := manager.Release(first.ID)
	if err != nil || released.State != Released {
		t.Fatalf("release: %+v %v", released, err)
	}
	if len(recorder.ended) != 1 || recorder.ended[0].ID != first.ID {
		t.Fatalf("hooks saw %+v", recorder.ended)
	}
	if _, err := manager.Admit(trainingRequest("run_1"), "host_a"); err != nil {
		t.Fatalf("admit after release: %v", err)
	}
	if _, err := manager.Release(first.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("double release: got %v", err)
	}
}

func TestActiveListsLeasesPerRun(t *testing.T) {
	manager, _ := newTestManager(t, 4)
	manager.Admit(trainingRequest("run_1"), "host_a")
	manager.Admit(trainingRequest("run_2"), "host_a")
	manager.Admit(trainingRequest("run_1"), "host_b")
	if got := len(manager.Active("run_1")); got != 2 {
		t.Fatalf("run_1 leases: got %d, want 2", got)
	}
	if got := len(manager.Active("")); got != 3 {
		t.Fatalf("all leases: got %d, want 3", got)
	}
}

func runReaperUntilDone(ctx context.Context, manager *Manager, done chan<- struct{}) {
	manager.RunReaper(ctx, time.Millisecond)
	close(done)
}

func TestReaperLoopStopsWithItsContext(t *testing.T) {
	manager, fakeClock := newTestManager(t, 4)
	manager.Admit(trainingRequest("run_1"), "host_a")
	fakeClock.Advance(2 * time.Minute)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go runReaperUntilDone(ctx, manager, done)
	deadline := time.Now().Add(2 * time.Second)
	for len(manager.Active("")) > 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	cancel()
	<-done
	if len(manager.Active("")) != 0 {
		t.Fatal("the reaper loop did not reap the expired lease")
	}
}
