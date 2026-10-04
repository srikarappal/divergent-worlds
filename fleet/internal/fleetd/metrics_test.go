package fleetd

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"rlworld/fleet/internal/admission"
	"rlworld/fleet/internal/clock"
	"rlworld/fleet/internal/lease"
	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/scheduler"
	"rlworld/fleet/internal/vmm/fake"
)

const pool = `pool="firecracker"`

// scrape reads fleetd's /metrics into sample values keyed by metric name and labels.
func scrape(t *testing.T, serverURL string) map[string]float64 {
	t.Helper()
	response, err := http.Get(serverURL + "/metrics")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if !strings.HasPrefix(response.Header.Get("Content-Type"), "text/plain; version=0.0.4") {
		t.Fatalf("content type %q", response.Header.Get("Content-Type"))
	}
	samples := map[string]float64{}
	scanner := bufio.NewScanner(response.Body)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		separator := strings.LastIndex(line, " ")
		value, err := strconv.ParseFloat(line[separator+1:], 64)
		if err != nil {
			t.Fatalf("sample %q: %v", line, err)
		}
		samples[line[:separator]] = value
	}
	return samples
}

func clickBody() map[string]any {
	return map[string]any{"action": map[string]any{"name": "click", "x": 3, "y": 4}}
}

func TestLeasesAndStepsShowUpInFleetdMetrics(t *testing.T) {
	fleet := newTestFleet(t, 4, 64_000)
	status, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(2))
	if status != http.StatusCreated {
		t.Fatalf("lease %d %v", status, payload)
	}
	ids := leaseIDs(t, payload)
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+ids[0]+"/step", clickBody()); status != http.StatusOK {
		t.Fatalf("step %d", status)
	}
	samples := scrape(t, fleet.server.URL)
	expected := map[string]float64{
		"rlworld_fleet_lease_seconds_count{" + pool + "}":                      1,
		"rlworld_fleet_leases{" + pool + "}":                                   2,
		"rlworld_fleet_step_seconds_count{" + pool + `,part="env"}`:            1,
		"rlworld_fleet_step_seconds_count{" + pool + `,part="settle"}`:         1,
		"rlworld_fleet_lease_seconds_bucket{" + pool + `,le="+Inf"}`:           1,
		"rlworld_fleet_step_seconds_bucket{" + pool + `,part="env",le="+Inf"}`: 1,
	}
	for key, value := range expected {
		if samples[key] != value {
			t.Fatalf("%s = %v, want %v in %v", key, samples[key], value, samples)
		}
	}
	if status, _ := fleet.call(t, http.MethodDelete, "/v1/leases/"+ids[0], nil); status != http.StatusNoContent {
		t.Fatalf("close %d", status)
	}
	if left := scrape(t, fleet.server.URL)["rlworld_fleet_leases{"+pool+"}"]; left != 1 {
		t.Fatalf("leases after a close %v", left)
	}
}

func TestRestoreAndStepFailuresAreCounted(t *testing.T) {
	fleet := newTestFleet(t, 4, 64_000)
	fleet.driver.FailNext["restore"] = errors.New("snapshot unreadable")
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1)); status < 500 {
		t.Fatalf("a failed restore answered %d", status)
	}
	_, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	fleet.driver.FailNext["resume"] = errors.New("the VM is gone")
	stepPath := "/v1/leases/" + leaseIDs(t, payload)[0] + "/step"
	if status, _ := fleet.call(t, http.MethodPost, stepPath, clickBody()); status < 500 {
		t.Fatalf("a failed step answered %d", status)
	}
	samples := scrape(t, fleet.server.URL)
	if samples["rlworld_fleet_restore_failures_total{"+pool+"}"] != 1 ||
		samples["rlworld_fleet_step_failures_total{"+pool+"}"] != 1 || samples["rlworld_fleet_leases{"+pool+"}"] != 1 {
		t.Fatalf("failures %v", samples)
	}
}

type catalogMetric struct {
	Name    string   `json:"name"`
	Type    string   `json:"type"`
	Service string   `json:"service"`
	Labels  []string `json:"labels"`
}

func TestTheMetricsCatalogListsExactlyWhatFleetdExposes(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "schemas", "metrics.json"))
	if err != nil {
		t.Fatal(err)
	}
	var catalog struct {
		Metrics []catalogMetric `json:"metrics"`
	}
	if err := json.Unmarshal(raw, &catalog); err != nil {
		t.Fatal(err)
	}
	exposed := newTestFleet(t, 1, 4096).service.Metrics().Families()
	listed := 0
	for _, metric := range catalog.Metrics {
		if metric.Service != "fleetd" {
			continue
		}
		listed++
		if !slices.Equal(exposed[metric.Name], append([]string{metric.Type}, metric.Labels...)) {
			t.Errorf("%s: fleetd exposes %v, the catalog lists %s %v", metric.Name, exposed[metric.Name],
				metric.Type, metric.Labels)
		}
	}
	if listed != len(exposed) {
		t.Fatalf("the catalog lists %d fleetd metrics, fleetd exposes %d", listed, len(exposed))
	}
}

// dyingGuest answers until the host dies, then every call into its VMs fails.
type dyingGuest struct {
	*stubGuest
	dead atomic.Bool
}

var errHostDown = errors.New("the host stopped answering")

func (guest *dyingGuest) Act(ctx context.Context, vmID string, action json.RawMessage) error {
	if guest.dead.Load() {
		return errHostDown
	}
	return guest.stubGuest.Act(ctx, vmID, action)
}

func (guest *dyingGuest) ScreenHash(ctx context.Context, vmID string) (string, error) {
	if guest.dead.Load() {
		return "", errHostDown
	}
	return guest.stubGuest.ScreenHash(ctx, vmID)
}

func (guest *dyingGuest) Extract(ctx context.Context, vmID string, apps []string) (json.RawMessage, error) {
	if guest.dead.Load() {
		return nil, errHostDown
	}
	return guest.stubGuest.Extract(ctx, vmID, apps)
}

func TestAHostDyingMidRunFailsItsEnvsWhileTheirLeasesStillFreeCapacity(t *testing.T) {
	fakeClock := clock.NewFake(time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC))
	controller, err := admission.NewController(admission.Config{Capacity: 3})
	if err != nil {
		t.Fatal(err)
	}
	leases := lease.NewManager(controller, fakeClock, time.Minute)
	placement := scheduler.New()
	placement.UpsertHost(scheduler.Host{ID: "host_a", Pool: "firecracker",
		Capacity: scheduler.Resources{CPUMillicores: 64_000, MemoryMiB: 64_000}})
	guest := &dyingGuest{stubGuest: &stubGuest{}}
	agent := nodeagent.New(nodeagent.Config{HostID: "host_a", OverlayDir: "/overlays", SettleStable: time.Millisecond,
		SettlePoll: time.Millisecond}, fake.New(), guest, nopCommands{}, fakeClock, fakeClock.Advance)
	service := NewService(leases, placement, map[string]Node{"host_a": agent}, "/checkpoints")
	server := httptest.NewServer(NewHandler(service))
	defer server.Close()
	fleet := &testFleet{server: server, service: service}
	_, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(3))
	ids := leaseIDs(t, payload)
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+ids[0]+"/step", clickBody()); status != http.StatusOK {
		t.Fatalf("step before the host died %d", status)
	}
	guest.dead.Store(true)
	for _, leaseID := range ids {
		if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/step", clickBody()); status < 500 {
			t.Fatalf("step on a dead host answered %d", status)
		}
	}
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+ids[0]+"/finish",
		map[string]any{"apps": []string{"mail"}}); status < 500 {
		t.Fatalf("extraction on a dead host answered %d", status)
	}
	if status, _ := fleet.call(t, http.MethodDelete, "/v1/leases/"+ids[0], nil); status != http.StatusNoContent {
		t.Fatalf("closing on a dead host %d", status)
	}
	fakeClock.Advance(2 * time.Minute)
	if reaped := leases.ReapExpired(); len(reaped) != 2 {
		t.Fatalf("abandoned leases reaped %d", len(reaped))
	}
	if inUse := controller.Status().Usage.Total; inUse != 0 {
		t.Fatalf("capacity still held %d", inUse)
	}
	samples := scrape(t, server.URL)
	if samples["rlworld_fleet_step_failures_total{"+pool+"}"] != 3 || samples["rlworld_fleet_leases{"+pool+"}"] != 0 {
		t.Fatalf("metrics after the host died %v", samples)
	}
	guest.dead.Store(false)
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(3)); status != http.StatusCreated {
		t.Fatalf("leasing after the host came back %d", status)
	}
}
