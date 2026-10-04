package fleetd

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/clock"
	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/snapshot"
)

var worldDigest = "sha256:" + strings.Repeat("3c", 32)

// worldGuest materializes a world in injectTime of world clock; with varies set its app state changes between
// loads.
type worldGuest struct {
	probeGuest
	clock      *clock.Fake
	injectTime time.Duration
	varies     bool
	mu         sync.Mutex
	loads      int
}

func (guest *worldGuest) Inject(context.Context, string, json.RawMessage) error {
	guest.clock.Advance(guest.injectTime)
	guest.mu.Lock()
	defer guest.mu.Unlock()
	guest.loads++
	return nil
}

func (guest *worldGuest) Extract(context.Context, string, []string) (json.RawMessage, error) {
	guest.mu.Lock()
	defer guest.mu.Unlock()
	if guest.varies {
		return json.RawMessage(`{"mail": {"inbox": [{"received": ` + strconv.Itoa(guest.loads) + `}]}}`), nil
	}
	return json.RawMessage(`{"files": {"invoices.csv": {"rows": 40}}, "mail": {"inbox": []}}`), nil
}

func worldFleet(t *testing.T, injectTime time.Duration, varies bool) (*snapshotFleet, *worldGuest, string) {
	t.Helper()
	fakeClock := clock.NewFake(time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC))
	guest := &worldGuest{probeGuest: probeGuest{&stubGuest{}}, clock: fakeClock, injectTime: injectTime,
		varies: varies}
	fleet := buildSnapshotFleet(t, fakePool, true, guest, fakeClock)
	_, built := fleet.call(t, http.MethodPost, "/v1/snapshots/build", buildBody(imageBuildDir(t, "fake", "vm")))
	return fleet, guest, built["digest"].(string)
}

func preparation() map[string]any {
	return map[string]any{"world": worldDigest, "payload": map[string]any{"world": map[string]any{"seed": 1842},
		"apps": []string{"mail", "files"}}, "apps": []string{"mail", "files"}, "pool": "firecracker",
		"memory_mib": 2048, "cpu_millicores": 2000, "vcpus": 2}
}

func TestAFastDeterministicWorldMaterializesAtEveryRestore(t *testing.T) {
	fleet, guest, base := worldFleet(t, 800*time.Millisecond, false)
	status, decision := fleet.call(t, http.MethodPost, "/v1/snapshots/"+base+"/worlds", preparation())
	if status != http.StatusOK || decision["snapshot"] != nil || decision["deterministic"] != true {
		t.Fatalf("decision: %d %v", status, decision)
	}
	if seconds := decision["materialize_seconds"].([]any); len(seconds) != 2 || seconds[0] != 0.8 {
		t.Fatalf("both loads are timed: %v", seconds)
	}
	if len(fleet.driver.Restores) != 2 || guest.loads != 2 {
		t.Fatalf("two fresh envs of the base: %d restores, %d loads", len(fleet.driver.Restores), guest.loads)
	}
	if envs := fleet.service.nodes["host_a"].(*nodeagent.Agent).Envs(); len(envs) != 0 {
		t.Fatalf("the trial envs are destroyed: %v", envs)
	}
	if free := fleet.scheduler.FreeResources("host_a"); free.MemoryMiB != 65_536 {
		t.Fatalf("the trial envs give their room back: %+v", free)
	}
	_, again := fleet.call(t, http.MethodPost, "/v1/snapshots/"+base+"/worlds", preparation())
	if len(fleet.driver.Restores) != 2 || again["decided_at"] != decision["decided_at"] {
		t.Fatalf("the decision is kept per base and world: %v", again)
	}
}

func TestASlowWorldGetsAWorldSnapshotThatLeasesRestore(t *testing.T) {
	fleet, _, base := worldFleet(t, 6*time.Second, false)
	_, decision := fleet.call(t, http.MethodPost, "/v1/snapshots/"+base+"/worlds", preparation())
	worldSnapshot, captured := decision["snapshot"].(string)
	if !captured || decision["deterministic"] != true {
		t.Fatalf("a slow world is snapshotted: %v", decision)
	}
	_, detail := fleet.call(t, http.MethodGet, "/v1/snapshots/"+worldSnapshot, nil)
	manifest := detail["manifest"].(map[string]any)
	if manifest["base"] != base || manifest["world"] != worldDigest || manifest["image"] != "noble_calc_v1" {
		t.Fatalf("the world snapshot names its base and world: %v", manifest)
	}
	stored, err := snapshot.LookupWorldDecision(context.Background(), fleet.store, base, worldDigest)
	if err != nil || stored.Snapshot != worldSnapshot {
		t.Fatalf("index: %+v %v", stored, err)
	}
	body := leaseBody(1)
	body["snapshot"] = map[string]string{"digest": worldSnapshot}
	if status, leased := fleet.call(t, http.MethodPost, "/v1/leases", body); status != http.StatusCreated {
		t.Fatalf("leasing the world snapshot: %d %v", status, leased)
	}
	key := "snapshots/" + strings.TrimPrefix(worldSnapshot, "sha256:") + ".json"
	if err := fleet.store.Delete(context.Background(), key); err != nil {
		t.Fatal(err)
	}
	restores := len(fleet.driver.Restores)
	_, redecided := fleet.call(t, http.MethodPost, "/v1/snapshots/"+base+"/worlds", preparation())
	if len(fleet.driver.Restores) != restores+2 || redecided["snapshot"] == nil {
		t.Fatalf("a world snapshot retention removed is decided and taken again: %v", redecided)
	}
}

func TestAWorldThatLoadsDifferentlyEachTimeIsSnapshotted(t *testing.T) {
	fleet, _, base := worldFleet(t, 0, true)
	_, decision := fleet.call(t, http.MethodPost, "/v1/snapshots/"+base+"/worlds", preparation())
	if decision["deterministic"] != false || decision["snapshot"] == nil {
		t.Fatalf("a nondeterministic world is fixed in a snapshot: %v", decision)
	}
}

func TestWorldPreparationsNeedAKnownBaseAndAFullRequest(t *testing.T) {
	fleet, _, base := worldFleet(t, 0, false)
	partial := preparation()
	delete(partial, "payload")
	if status, answer := fleet.call(t, http.MethodPost, "/v1/snapshots/"+base+"/worlds", partial); status != 400 {
		t.Fatalf("a preparation without its payload: %d %v", status, answer)
	}
	unknown := "sha256:" + strings.Repeat("ee", 32)
	if status, answer := fleet.call(t, http.MethodPost, "/v1/snapshots/"+unknown+"/worlds",
		preparation()); status != http.StatusNotFound || answer["code"] != "unknown_snapshot" {
		t.Fatalf("an unknown base: %d %v", status, answer)
	}
	crowded := preparation()
	crowded["memory_mib"] = 60_000
	if status, answer := fleet.call(t, http.MethodPost, "/v1/snapshots/"+base+"/worlds",
		crowded); status != http.StatusServiceUnavailable || answer["code"] != "no_capacity" {
		t.Fatalf("no room for two trial envs: %d %v", status, answer)
	}
	off := newSnapshotFleet(t, fakePool, false)
	if status, _ := off.call(t, http.MethodPost, "/v1/snapshots/"+base+"/worlds", preparation()); status != 503 {
		t.Fatalf("without a snapshot store: %d", status)
	}
}
