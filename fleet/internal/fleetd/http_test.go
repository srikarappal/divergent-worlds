package fleetd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/admission"
	"rlworld/fleet/internal/clock"
	"rlworld/fleet/internal/lease"
	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/scheduler"
	"rlworld/fleet/internal/vmm"
	"rlworld/fleet/internal/vmm/fake"
)

type stubGuest struct {
	mu       sync.Mutex
	injected []string
}

func (guest *stubGuest) Act(context.Context, string, json.RawMessage) error { return nil }
func (guest *stubGuest) ScreenHash(context.Context, string) (string, error) {
	return "steady", nil
}
func (guest *stubGuest) Observe(_ context.Context, vmID string) (json.RawMessage, error) {
	return json.RawMessage(`{"env": "` + vmID + `"}`), nil
}
func (guest *stubGuest) Inject(_ context.Context, _ string, payload json.RawMessage) error {
	guest.mu.Lock()
	defer guest.mu.Unlock()
	guest.injected = append(guest.injected, string(payload))
	return nil
}
func (guest *stubGuest) Extract(_ context.Context, _ string, apps []string) (json.RawMessage, error) {
	return json.RawMessage(`{"mail": {"sent": []}, "apps": "` + strings.Join(apps, ",") + `"}`), nil
}

type nopCommands struct{}

func (nopCommands) Run(context.Context, []string) error { return nil }

type testFleet struct {
	server    *httptest.Server
	driver    *fake.Driver
	guest     *stubGuest
	clock     *clock.Fake
	leases    *lease.Manager
	scheduler *scheduler.Scheduler
}

func newTestFleet(t *testing.T, capacity int, hostMemoryMiB int) *testFleet {
	t.Helper()
	fakeClock := clock.NewFake(time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC))
	controller, err := admission.NewController(admission.Config{Capacity: capacity})
	if err != nil {
		t.Fatal(err)
	}
	leases := lease.NewManager(controller, fakeClock, time.Minute)
	placement := scheduler.New()
	placement.UpsertHost(scheduler.Host{ID: "host_a", Pool: "firecracker",
		Capacity: scheduler.Resources{CPUMillicores: 64_000, MemoryMiB: hostMemoryMiB}})
	driver := fake.New()
	guest := &stubGuest{}
	agent := nodeagent.New(nodeagent.Config{HostID: "host_a", OverlayDir: "/overlays", SettleStable: time.Millisecond,
		SettlePoll: time.Millisecond}, driver, guest, nopCommands{}, fakeClock, fakeClock.Advance)
	service := NewService(leases, placement, map[string]Node{"host_a": agent}, "/checkpoints")
	server := httptest.NewServer(NewHandler(service))
	t.Cleanup(server.Close)
	return &testFleet{server: server, driver: driver, guest: guest, clock: fakeClock, leases: leases, scheduler: placement}
}

func (fleet *testFleet) call(t *testing.T, method, path string, body any) (int, map[string]any) {
	t.Helper()
	encoded, _ := json.Marshal(body)
	request, _ := http.NewRequest(method, fleet.server.URL+path, bytes.NewReader(encoded))
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var decoded map[string]any
	_ = json.NewDecoder(response.Body).Decode(&decoded)
	return response.StatusCode, decoded
}

func leaseBody(groupSize int) map[string]any {
	return map[string]any{
		"run_id": "run_1", "tenant": "lab", "cost_center": "research", "pool": "firecracker",
		"priority": "training", "endpoint": "policy", "group_size": groupSize, "memory_mib": 2048,
		"cpu_millicores": 2000, "vcpus": 2,
		"snapshot": map[string]string{"digest": "sha256:snap", "state_path": "/s/base.state",
			"memory_path": "/s/base.memory", "base_disk_path": "/s/base.ext4"},
	}
}

func leaseIDs(t *testing.T, payload map[string]any) []string {
	t.Helper()
	var ids []string
	for _, entry := range payload["leases"].([]any) {
		ids = append(ids, entry.(map[string]any)["lease_id"].(string))
	}
	return ids
}

func TestAGroupLeaseStepsFinishesAndCloses(t *testing.T) {
	fleet := newTestFleet(t, 10, 100_000)
	status, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(2))
	if status != http.StatusCreated {
		t.Fatalf("lease status %d %v", status, payload)
	}
	ids := leaseIDs(t, payload)
	if len(ids) != 2 {
		t.Fatalf("leases %v", ids)
	}
	status, stepPayload := fleet.call(t, http.MethodPost, "/v1/leases/"+ids[0]+"/step",
		map[string]any{"action": map[string]any{"name": "click", "x": 3, "y": 4}})
	if status != http.StatusOK || stepPayload["observation"].(map[string]any)["env"] != "env_00000001" {
		t.Fatalf("step %d %v", status, stepPayload)
	}
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+ids[0]+"/heartbeat", nil); status != http.StatusOK {
		t.Fatalf("heartbeat status %d", status)
	}
	status, finishPayload := fleet.call(t, http.MethodPost, "/v1/leases/"+ids[0]+"/finish",
		map[string]any{"apps": []string{"mail", "files"}})
	if status != http.StatusOK || finishPayload["frozen"] != true ||
		finishPayload["final_state"].(map[string]any)["apps"] != "mail,files" {
		t.Fatalf("finish %d %v", status, finishPayload)
	}
	status, frozenPayload := fleet.call(t, http.MethodPost, "/v1/leases/"+ids[0]+"/step", map[string]any{"action": map[string]any{}})
	if status != http.StatusConflict || frozenPayload["code"] != "input_frozen" {
		t.Fatalf("step after finish %d %v", status, frozenPayload)
	}
	if status, _ := fleet.call(t, http.MethodDelete, "/v1/leases/"+ids[0], nil); status != http.StatusNoContent {
		t.Fatalf("close status %d", status)
	}
	if fleet.driver.State("env_00000001") != vmm.Destroyed {
		t.Fatal("closing a lease must destroy its env")
	}
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+ids[0]+"/step", map[string]any{}); status != http.StatusNotFound {
		t.Fatalf("step on a closed lease: %d", status)
	}
}

func TestAdmissionAndCapacityErrorsMapToStatusCodes(t *testing.T) {
	fullAdmission := newTestFleet(t, 1, 100_000)
	status, payload := fullAdmission.call(t, http.MethodPost, "/v1/leases", leaseBody(2))
	if status != http.StatusTooManyRequests || payload["code"] != "fleet_full" {
		t.Fatalf("admission %d %v", status, payload)
	}
	if free := fullAdmission.scheduler.FreeResources("host_a").MemoryMiB; free != 100_000 {
		t.Fatalf("a rejected group leaked %d MiB", 100_000-free)
	}
	smallHost := newTestFleet(t, 10, 2048)
	status, payload = smallHost.call(t, http.MethodPost, "/v1/leases", leaseBody(2))
	if status != http.StatusServiceUnavailable || payload["code"] != "no_capacity" {
		t.Fatalf("placement %d %v", status, payload)
	}
}

func TestForkLeasesClonesOnTheSameHost(t *testing.T) {
	fleet := newTestFleet(t, 10, 100_000)
	_, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	source := leaseIDs(t, payload)[0]
	status, forkPayload := fleet.call(t, http.MethodPost, "/v1/leases/"+source+"/fork", map[string]any{"count": 2})
	if status != http.StatusCreated || len(leaseIDs(t, forkPayload)) != 2 {
		t.Fatalf("fork %d %v", status, forkPayload)
	}
	_, listed := fleet.call(t, http.MethodGet, "/v1/leases?run_id=run_1", nil)
	if len(leaseIDs(t, listed)) != 3 {
		t.Fatalf("listed %v", listed)
	}
	status, checkpoint := fleet.call(t, http.MethodPost, "/v1/leases/"+source+"/checkpoint", nil)
	if status != http.StatusOK || checkpoint["kind"] != "full" {
		t.Fatalf("checkpoint %d %v", status, checkpoint)
	}
}

func TestResetStartsAFreshEnvAndUnfreezes(t *testing.T) {
	fleet := newTestFleet(t, 10, 100_000)
	_, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	leaseID := leaseIDs(t, payload)[0]
	fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/finish", map[string]any{"apps": []string{"mail"}})
	status, resetPayload := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/reset", nil)
	if status != http.StatusOK || resetPayload["observation"].(map[string]any)["env"] != "env_00000002" {
		t.Fatalf("reset %d %v", status, resetPayload)
	}
	if fleet.driver.State("env_00000001") != vmm.Destroyed {
		t.Fatal("reset must destroy the old env")
	}
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/step", map[string]any{"action": map[string]any{}}); status != http.StatusOK {
		t.Fatalf("step after reset: %d", status)
	}
}

func TestExpiredLeasesDestroyTheirEnvs(t *testing.T) {
	fleet := newTestFleet(t, 10, 100_000)
	_, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	leaseID := leaseIDs(t, payload)[0]
	fleet.clock.Advance(2 * time.Minute)
	if reaped := fleet.leases.ReapExpired(); len(reaped) != 1 || reaped[0].ID != leaseID {
		t.Fatalf("reaped %+v", reaped)
	}
	if fleet.driver.State("env_00000001") != vmm.Destroyed {
		t.Fatal("an expired lease must destroy its env")
	}
	if free := fleet.scheduler.FreeResources("host_a").MemoryMiB; free != 100_000 {
		t.Fatalf("an expired lease must free host capacity, %d MiB free", free)
	}
}

func TestInjectAndBadRequests(t *testing.T) {
	fleet := newTestFleet(t, 10, 100_000)
	_, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	leaseID := leaseIDs(t, payload)[0]
	status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/inject",
		map[string]any{"browser_cookies": map[string]string{"session_crm": "token"}})
	if status != http.StatusNoContent || len(fleet.guest.injected) != 1 {
		t.Fatalf("inject %d %v", status, fleet.guest.injected)
	}
	request, _ := http.NewRequest(http.MethodPost, fleet.server.URL+"/v1/leases", strings.NewReader("{not json"))
	response, err := http.DefaultClient.Do(request)
	if err != nil || response.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad json: %v %v", response.StatusCode, err)
	}
	if status, healthy := fleet.call(t, http.MethodGet, "/healthz", nil); status != http.StatusOK || healthy["status"] != "ok" {
		t.Fatalf("health %d %v", status, healthy)
	}
}

func TestUnknownErrorsAreEnvErrors(t *testing.T) {
	status, code := statusFor(errors.New("vm crashed"))
	if status != http.StatusInternalServerError || code != "env_error" {
		t.Fatalf("unknown errors must be env errors: %d %s", status, code)
	}
}

func TestAdmissionStatusAndTenantQuotas(t *testing.T) {
	fleet := newTestFleet(t, 8, 64_000)
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(2)); status != http.StatusCreated {
		t.Fatalf("lease: %d", status)
	}
	status, payload := fleet.call(t, http.MethodPut, "/v1/admission/tenants/lab", map[string]int{"quota": 2})
	if status != http.StatusOK || payload["tenant_quotas"].(map[string]any)["lab"].(float64) != 2 {
		t.Fatalf("set quota: %d %v", status, payload)
	}
	if inUse := payload["usage"].(map[string]any)["by_tenant"].(map[string]any)["lab"].(float64); inUse != 2 {
		t.Fatalf("lab in use: %v", inUse)
	}
	if status, payload = fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1)); status != http.StatusTooManyRequests ||
		payload["code"] != "tenant_quota" {
		t.Fatalf("lease past the quota: %d %v", status, payload)
	}
	if status, _ = fleet.call(t, http.MethodPut, "/v1/admission/tenants/lab", map[string]int{"quota": -1}); status !=
		http.StatusBadRequest {
		t.Fatalf("negative quota: %d", status)
	}
	if status, payload = fleet.call(t, http.MethodGet, "/v1/admission", nil); status != http.StatusOK ||
		payload["capacity"].(float64) != 8 {
		t.Fatalf("status: %d %v", status, payload)
	}
}

type manifestOperation struct {
	Name    string `json:"name"`
	Service string `json:"service"`
	Method  string `json:"method"`
	Path    string `json:"path"`
}

func TestConsoleOperationsServedByFleetdAreRouted(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "schemas", "api_operations.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Operations []manifestOperation `json:"operations"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	patterns := map[string]bool{}
	for _, entry := range (&httpAPI{}).routes() {
		patterns[entry.pattern] = true
	}
	checked := 0
	for _, operation := range manifest.Operations {
		if operation.Service != "fleetd" {
			continue
		}
		checked++
		if !patterns[operation.Method+" "+operation.Path] {
			t.Errorf("%s: fleetd serves no %s %s", operation.Name, operation.Method, operation.Path)
		}
	}
	if checked == 0 {
		t.Fatal("the manifest names no fleetd operation")
	}
}
