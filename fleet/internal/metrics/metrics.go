// Package metrics keeps counters, gauges and histograms and writes them in the Prometheus text format
// (DESIGN.md section 6.15).
//
// Families are written in the order they were registered and series in the
// order of their label values, so a scrape reads the same every time. Gauges
// computed at scrape time come from a collector function.
package metrics

import (
	"bufio"
	"fmt"
	"io"
	"math"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
)

// LatencyBuckets suit latencies from milliseconds to a minute.
var LatencyBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60}

// Sample is one computed gauge value.
type Sample struct {
	LabelValues []string
	Value       float64
}

// Collector computes a gauge family's samples at scrape time.
type Collector func() []Sample

type series struct {
	labelValues  []string
	value        float64
	bucketCounts []uint64
	count        uint64
}

type family struct {
	name       string
	help       string
	kind       string
	labelNames []string
	buckets    []float64
	series     map[string]*series
	collect    Collector
}

// Registry holds every metric family one service exposes.
type Registry struct {
	mu       sync.Mutex
	families []*family
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry {
	return &Registry{}
}

func (registry *Registry) add(created *family) *family {
	registry.mu.Lock()
	defer registry.mu.Unlock()
	for _, existing := range registry.families {
		if existing.name == created.name {
			panic("metric " + created.name + " is registered twice")
		}
	}
	registry.families = append(registry.families, created)
	return created
}

// Counter registers a monotonically increasing family.
func (registry *Registry) Counter(name, help string, labelNames ...string) *Counter {
	return &Counter{registry: registry, family: registry.add(&family{name: name, help: help, kind: "counter",
		labelNames: labelNames, series: map[string]*series{}})}
}

// Gauge registers a family that is set to its current value.
func (registry *Registry) Gauge(name, help string, labelNames ...string) *Gauge {
	return &Gauge{registry: registry, family: registry.add(&family{name: name, help: help, kind: "gauge",
		labelNames: labelNames, series: map[string]*series{}})}
}

// GaugeFunc registers a gauge family computed by collect at every scrape.
func (registry *Registry) GaugeFunc(name, help string, collect Collector, labelNames ...string) {
	registry.add(&family{name: name, help: help, kind: "gauge", labelNames: labelNames, collect: collect})
}

// Histogram registers a family that counts observations into cumulative buckets.
func (registry *Registry) Histogram(name, help string, buckets []float64, labelNames ...string) *Histogram {
	return &Histogram{registry: registry, family: registry.add(&family{name: name, help: help, kind: "histogram",
		labelNames: labelNames, buckets: slices.Sorted(slices.Values(buckets)), series: map[string]*series{}})}
}

// seriesFor finds or creates the series of labelValues; the caller holds the registry lock.
func (target *family) seriesFor(labelValues []string) *series {
	if len(labelValues) != len(target.labelNames) {
		panic(fmt.Sprintf("metric %s takes %d label values, got %d", target.name, len(target.labelNames),
			len(labelValues)))
	}
	key := strings.Join(labelValues, "\x00")
	found, exists := target.series[key]
	if !exists {
		found = &series{labelValues: slices.Clone(labelValues), bucketCounts: make([]uint64, len(target.buckets))}
		target.series[key] = found
	}
	return found
}

// Counter is a family of counters.
type Counter struct {
	registry *Registry
	family   *family
}

// Add increases the series of labelValues; counters never go down.
func (counter *Counter) Add(amount float64, labelValues ...string) {
	if amount < 0 {
		panic("counter " + counter.family.name + " cannot decrease")
	}
	counter.registry.mu.Lock()
	defer counter.registry.mu.Unlock()
	counter.family.seriesFor(labelValues).value += amount
}

// Inc adds one.
func (counter *Counter) Inc(labelValues ...string) {
	counter.Add(1, labelValues...)
}

// Gauge is a family of gauges.
type Gauge struct {
	registry *Registry
	family   *family
}

// Set sets the series of labelValues.
func (gauge *Gauge) Set(value float64, labelValues ...string) {
	gauge.registry.mu.Lock()
	defer gauge.registry.mu.Unlock()
	gauge.family.seriesFor(labelValues).value = value
}

// Histogram is a family of histograms.
type Histogram struct {
	registry *Registry
	family   *family
}

// Observe counts one observation of the series of labelValues.
func (histogram *Histogram) Observe(value float64, labelValues ...string) {
	histogram.registry.mu.Lock()
	defer histogram.registry.mu.Unlock()
	observed := histogram.family.seriesFor(labelValues)
	for index, bound := range histogram.family.buckets {
		if value <= bound {
			observed.bucketCounts[index]++
		}
	}
	observed.count++
	observed.value += value
}

func escapeLabelValue(value string) string {
	return strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`).Replace(value)
}

func escapeHelp(help string) string {
	return strings.NewReplacer(`\`, `\\`, "\n", `\n`).Replace(help)
}

func formatValue(value float64) string {
	if math.IsInf(value, 1) {
		return "+Inf"
	}
	return strconv.FormatFloat(value, 'g', -1, 64)
}

func labelText(names, values []string, extraName, extraValue string) string {
	var pairs []string
	for index, name := range names {
		pairs = append(pairs, name+`="`+escapeLabelValue(values[index])+`"`)
	}
	if extraName != "" {
		pairs = append(pairs, extraName+`="`+extraValue+`"`)
	}
	if len(pairs) == 0 {
		return ""
	}
	return "{" + strings.Join(pairs, ",") + "}"
}

func sortedSeries(target *family) []*series {
	var all []*series
	for _, found := range target.series {
		all = append(all, found)
	}
	slices.SortFunc(all, compareSeries)
	return all
}

func compareSeries(left, right *series) int {
	return slices.Compare(left.labelValues, right.labelValues)
}

func writeFamily(writer *bufio.Writer, target *family) {
	fmt.Fprintf(writer, "# HELP %s %s\n# TYPE %s %s\n", target.name, escapeHelp(target.help), target.name, target.kind)
	if target.collect != nil {
		for _, sample := range target.collect() {
			fmt.Fprintf(writer, "%s%s %s\n", target.name, labelText(target.labelNames, sample.LabelValues, "", ""),
				formatValue(sample.Value))
		}
		return
	}
	for _, found := range sortedSeries(target) {
		if target.kind != "histogram" {
			fmt.Fprintf(writer, "%s%s %s\n", target.name, labelText(target.labelNames, found.labelValues, "", ""),
				formatValue(found.value))
			continue
		}
		for index, bound := range target.buckets {
			fmt.Fprintf(writer, "%s_bucket%s %d\n", target.name,
				labelText(target.labelNames, found.labelValues, "le", formatValue(bound)), found.bucketCounts[index])
		}
		fmt.Fprintf(writer, "%s_bucket%s %d\n", target.name,
			labelText(target.labelNames, found.labelValues, "le", "+Inf"), found.count)
		labels := labelText(target.labelNames, found.labelValues, "", "")
		fmt.Fprintf(writer, "%s_sum%s %s\n%s_count%s %d\n", target.name, labels, formatValue(found.value),
			target.name, labels, found.count)
	}
}

// Write writes every family in the Prometheus text exposition format.
func (registry *Registry) Write(output io.Writer) error {
	writer := bufio.NewWriter(output)
	registry.mu.Lock()
	families := slices.Clone(registry.families)
	registry.mu.Unlock()
	for _, target := range families {
		if target.collect != nil {
			writeFamily(writer, target)
			continue
		}
		registry.mu.Lock()
		writeFamily(writer, target)
		registry.mu.Unlock()
	}
	return writer.Flush()
}

// Families names each registered family with its kind and label names, for parity checks against the catalog.
func (registry *Registry) Families() map[string][]string {
	registry.mu.Lock()
	defer registry.mu.Unlock()
	described := map[string][]string{}
	for _, target := range registry.families {
		described[target.name] = append([]string{target.kind}, target.labelNames...)
	}
	return described
}

// ServeHTTP answers a Prometheus scrape.
func (registry *Registry) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	writer.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	if err := registry.Write(writer); err != nil {
		return
	}
}
