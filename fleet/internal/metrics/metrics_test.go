package metrics

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func written(t *testing.T, registry *Registry) string {
	t.Helper()
	var output bytes.Buffer
	if err := registry.Write(&output); err != nil {
		t.Fatal(err)
	}
	return output.String()
}

func TestCountersAndGaugesWriteOneLinePerSeriesInLabelOrder(t *testing.T) {
	registry := NewRegistry()
	failures := registry.Counter("rlworld_fleet_restore_failures_total", "Envs whose restore failed", "pool")
	failures.Inc("windows")
	failures.Add(2, "linux")
	failures.Inc("linux")
	fidelity := registry.Gauge("rlworld_clone_fidelity", "Clone fidelity", "app")
	fidelity.Set(0.8, "crm")
	fidelity.Set(0.95, "crm")
	unlabeled := registry.Gauge("rlworld_verifier_recall", "Verifier recall")
	unlabeled.Set(0.5)
	expected := "# HELP rlworld_fleet_restore_failures_total Envs whose restore failed\n" +
		"# TYPE rlworld_fleet_restore_failures_total counter\n" +
		"rlworld_fleet_restore_failures_total{pool=\"linux\"} 3\n" +
		"rlworld_fleet_restore_failures_total{pool=\"windows\"} 1\n" +
		"# HELP rlworld_clone_fidelity Clone fidelity\n# TYPE rlworld_clone_fidelity gauge\n" +
		"rlworld_clone_fidelity{app=\"crm\"} 0.95\n" +
		"# HELP rlworld_verifier_recall Verifier recall\n# TYPE rlworld_verifier_recall gauge\n" +
		"rlworld_verifier_recall 0.5\n"
	if got := written(t, registry); got != expected {
		t.Fatalf("got\n%s\nwant\n%s", got, expected)
	}
}

func TestHistogramsCountCumulativeBucketsSumsAndCounts(t *testing.T) {
	registry := NewRegistry()
	latency := registry.Histogram("rlworld_fleet_step_seconds", "Step latency", []float64{1, 0.1}, "pool", "part")
	latency.Observe(0.05, "linux", "env")
	latency.Observe(0.5, "linux", "env")
	latency.Observe(3, "linux", "env")
	expected := "# HELP rlworld_fleet_step_seconds Step latency\n# TYPE rlworld_fleet_step_seconds histogram\n" +
		"rlworld_fleet_step_seconds_bucket{pool=\"linux\",part=\"env\",le=\"0.1\"} 1\n" +
		"rlworld_fleet_step_seconds_bucket{pool=\"linux\",part=\"env\",le=\"1\"} 2\n" +
		"rlworld_fleet_step_seconds_bucket{pool=\"linux\",part=\"env\",le=\"+Inf\"} 3\n" +
		"rlworld_fleet_step_seconds_sum{pool=\"linux\",part=\"env\"} 3.55\n" +
		"rlworld_fleet_step_seconds_count{pool=\"linux\",part=\"env\"} 3\n"
	if got := written(t, registry); got != expected {
		t.Fatalf("got\n%s\nwant\n%s", got, expected)
	}
}

// poolCounts stands in for a service computing a gauge at scrape time.
type poolCounts struct {
	samples []Sample
}

func (counts *poolCounts) collect() []Sample {
	return counts.samples
}

func TestComputedGaugesAreCollectedAtEveryScrape(t *testing.T) {
	registry := NewRegistry()
	counts := &poolCounts{}
	registry.GaugeFunc("rlworld_fleet_leases", "Leases held", counts.collect, "pool")
	empty := "# HELP rlworld_fleet_leases Leases held\n# TYPE rlworld_fleet_leases gauge\n"
	if got := written(t, registry); got != empty {
		t.Fatalf("empty gauge %q", got)
	}
	counts.samples = []Sample{{LabelValues: []string{"linux"}, Value: 7}}
	if got := written(t, registry); !strings.HasSuffix(got, "rlworld_fleet_leases{pool=\"linux\"} 7\n") {
		t.Fatalf("live gauge %q", got)
	}
}

func TestLabelValuesAndHelpAreEscaped(t *testing.T) {
	registry := NewRegistry()
	registry.Counter("rlworld_episodes_total", "Episodes\nby image", "image").Inc("noble \"calc\"\\v1\n")
	got := written(t, registry)
	if !strings.Contains(got, "# HELP rlworld_episodes_total Episodes\\nby image\n") ||
		!strings.Contains(got, `rlworld_episodes_total{image="noble \"calc\"\\v1\n"} 1`) {
		t.Fatalf("escaping %q", got)
	}
}

func TestMisuseIsCaughtWhenItHappens(t *testing.T) {
	registry := NewRegistry()
	counter := registry.Counter("rlworld_a_total", "a", "pool")
	for name, misuse := range map[string]func(){
		"duplicate": registry.duplicate, "label count": counter.withoutLabels, "negative": counter.negative,
	} {
		if !panics(misuse) {
			t.Fatalf("%s did not panic", name)
		}
	}
}

func (registry *Registry) duplicate() {
	registry.Gauge("rlworld_a_total", "again")
}

func (counter *Counter) withoutLabels() {
	counter.Inc()
}

func (counter *Counter) negative() {
	counter.Add(-1, "linux")
}

func panics(call func()) (panicked bool) {
	defer recoverInto(&panicked)
	call()
	return false
}

func recoverInto(panicked *bool) {
	if recover() != nil {
		*panicked = true
	}
}

func TestScrapesGetTheTextFormat(t *testing.T) {
	registry := NewRegistry()
	registry.Counter("rlworld_b_total", "b").Inc()
	recorder := httptest.NewRecorder()
	registry.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if recorder.Header().Get("Content-Type") != "text/plain; version=0.0.4; charset=utf-8" ||
		!strings.Contains(recorder.Body.String(), "rlworld_b_total 1\n") {
		t.Fatalf("scrape %v %q", recorder.Header(), recorder.Body.String())
	}
	families := registry.Families()
	if len(families) != 1 || families["rlworld_b_total"][0] != "counter" {
		t.Fatalf("families %v", families)
	}
}
