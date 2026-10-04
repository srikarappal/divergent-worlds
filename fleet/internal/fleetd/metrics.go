package fleetd

import (
	"maps"
	"slices"

	"rlworld/fleet/internal/metrics"
	"rlworld/fleet/internal/nodeagent"
)

// fleetMetrics are the platform health signals fleetd owns (DESIGN.md section 6.15): lease latency and restore
// failures per pool, the env side of step latency split into act and observe time and the settle wait, failed
// steps and the leases held right now.
type fleetMetrics struct {
	registry        *metrics.Registry
	leaseSeconds    *metrics.Histogram
	restoreFailures *metrics.Counter
	stepSeconds     *metrics.Histogram
	stepFailures    *metrics.Counter
}

func newFleetMetrics(service *Service) *fleetMetrics {
	registry := metrics.NewRegistry()
	measured := &fleetMetrics{
		registry: registry,
		leaseSeconds: registry.Histogram("rlworld_fleet_lease_seconds",
			"Time to place, admit and start a lease group, by pool", metrics.LatencyBuckets, "pool"),
		restoreFailures: registry.Counter("rlworld_fleet_restore_failures_total",
			"Envs whose snapshot restore or start failed, by pool", "pool"),
		stepSeconds: registry.Histogram("rlworld_fleet_step_seconds",
			"Env side step latency split into the env's act and observe time and its settle wait, by pool",
			metrics.LatencyBuckets, "pool", "part"),
		stepFailures: registry.Counter("rlworld_fleet_step_failures_total",
			"Steps that failed in the env, by pool", "pool"),
	}
	registry.GaugeFunc("rlworld_fleet_leases", "Leases held right now, by pool", service.leasesByPool, "pool")
	return measured
}

// observeStep splits a step's env time into the settle wait and everything else.
func (measured *fleetMetrics) observeStep(pool string, result nodeagent.StepResult) {
	measured.stepSeconds.Observe((result.EnvDuration - result.SettleDuration).Seconds(), pool, "env")
	measured.stepSeconds.Observe(result.SettleDuration.Seconds(), pool, "settle")
}

// Metrics is the registry fleetd's /metrics route serves.
func (service *Service) Metrics() *metrics.Registry {
	return service.metrics.registry
}

func (service *Service) leasesByPool() []metrics.Sample {
	service.mu.Lock()
	counts := map[string]int{}
	for _, bound := range service.bindings {
		counts[bound.request.Pool]++
	}
	service.mu.Unlock()
	var samples []metrics.Sample
	for _, pool := range slices.Sorted(maps.Keys(counts)) {
		samples = append(samples, metrics.Sample{LabelValues: []string{pool}, Value: float64(counts[pool])})
	}
	return samples
}
