package fleetd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"rlworld/fleet/internal/admission"
	"rlworld/fleet/internal/clock"
	"rlworld/fleet/internal/lease"
	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/scheduler"
	"rlworld/fleet/internal/snapshot"
	"rlworld/fleet/internal/vmm/fake"
)

var (
	fakePool = snapshot.Compatibility{Pool: "firecracker", VMM: "fake", VMMVersion: "fake-1",
		CPUModel: "fleet_baseline"}
	imageDigest = "sha256:" + strings.Repeat("0a", 32)
)

// probeGuest is a guest whose Calc window is up and whose screen holds still.
type probeGuest struct {
	*stubGuest
}

func (probeGuest) Observe(context.Context, string, []string) (json.RawMessage, error) {
	return json.RawMessage(`{"accessibility": [{"role": "frame", "name": "Untitled 1 - LibreOffice Calc"}]}`), nil
}

// copyingCommands copies files for cp and ignores the network commands a laptop cannot run.
type copyingCommands struct{}

func (copyingCommands) Run(_ context.Context, argv []string) error {
	if argv[0] != "cp" {
		return nil
	}
	content, err := os.ReadFile(argv[len(argv)-2])
	if err != nil {
		return err
	}
	return os.WriteFile(argv[len(argv)-1], content, 0o644)
}

type snapshotFleet struct {
	server    *httptest.Server
	service   *Service
	driver    *fake.Driver
	store     snapshot.DirStore
	cache     *snapshot.Cache
	scheduler *scheduler.Scheduler
	workDir   string
}

func newSnapshotFleet(t *testing.T, binding snapshot.Compatibility, enabled bool) *snapshotFleet {
	t.Helper()
	fakeClock := clock.NewFake(time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC))
	controller, err := admission.NewController(admission.Config{Capacity: 16})
	if err != nil {
		t.Fatal(err)
	}
	leases := lease.NewManager(controller, fakeClock, time.Minute)
	placement := scheduler.New()
	placement.UpsertHost(scheduler.Host{ID: "host_a", Pool: "firecracker",
		Capacity: scheduler.Resources{CPUMillicores: 64_000, MemoryMiB: 65_536}})
	driver := fake.New()
	driver.WritesSnapshots = true
	store := snapshot.DirStore{Root: t.TempDir()}
	workDir := t.TempDir()
	cache := snapshot.NewCache(store, filepath.Join(workDir, "cache"), fakePool, 2)
	agent := nodeagent.New(nodeagent.Config{HostID: "host_a", OverlayDir: t.TempDir(), SettlePoll: 50 * time.Millisecond,
		Snapshots: cache}, driver, probeGuest{&stubGuest{}}, copyingCommands{}, fakeClock, fakeClock.Advance)
	service := NewService(leases, placement, map[string]Node{"host_a": agent}, t.TempDir())
	service.now = fakeClock.Now
	if enabled {
		service.EnableSnapshots(SnapshotSettings{Store: store, Compatibility: binding, WorkDir: workDir, ChunkSize: 16,
			Retention: snapshot.RetentionPolicy{MinAge: 24 * time.Hour, Grace: time.Hour}})
	}
	server := httptest.NewServer(NewHandler(service))
	t.Cleanup(server.Close)
	return &snapshotFleet{server: server, service: service, driver: driver, store: store, cache: cache,
		scheduler: placement, workDir: workDir}
}

func (fleet *snapshotFleet) call(t *testing.T, method, path string, body any) (int, map[string]any) {
	t.Helper()
	return (&testFleet{server: fleet.server}).call(t, method, path, body)
}

// imageBuildDir writes what rlworld-build-image leaves for a Linux image: a kernel, a root disk and manifest.json.
func imageBuildDir(t *testing.T, vmm, target string) string {
	t.Helper()
	directory := t.TempDir()
	outputs := map[string]any{}
	for role, name := range map[string]string{"kernel": "vmlinux", "rootfs": "rootfs.ext4"} {
		content := []byte(role + " bytes of noble_calc_v1")
		if err := os.WriteFile(filepath.Join(directory, name), content, 0o644); err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(content)
		outputs[role] = map[string]any{"path": name, "sha256": hex.EncodeToString(sum[:]), "bytes": len(content)}
	}
	manifest := map[string]any{
		"image": map[string]any{"id": "noble_calc_v1", "os": "linux",
			"pool":        map[string]any{"vmm": vmm, "cpu_model": "fleet_baseline"},
			"ready_probe": map[string]any{"window": "Calc", "screen_stable_ms": 200, "timeout_s": 60}},
		"image_digest": imageDigest, "target": target, "outputs": outputs,
	}
	encoded, _ := json.Marshal(manifest)
	if err := os.WriteFile(filepath.Join(directory, "manifest.json"), encoded, 0o644); err != nil {
		t.Fatal(err)
	}
	return directory
}

func buildBody(buildDir string) map[string]any {
	return map[string]any{"build_dir": buildDir, "memory_mib": 2048, "vcpus": 2}
}

func TestSnapshotsAreBuiltFromAnImageBuildAndBecomeCurrent(t *testing.T) {
	fleet := newSnapshotFleet(t, fakePool, true)
	status, built := fleet.call(t, http.MethodPost, "/v1/snapshots/build", buildBody(imageBuildDir(t, "fake", "vm")))
	if status != http.StatusCreated {
		t.Fatalf("build: %d %v", status, built)
	}
	digest := built["digest"].(string)
	if built["image"] != "noble_calc_v1" || built["image_digest"] != imageDigest || built["memory_mib"] != float64(2048) {
		t.Fatalf("manifest: %v", built)
	}
	boot := fleet.driver.Boots[0]
	if !strings.HasSuffix(boot.KernelPath, "vmlinux") || boot.VMID != "build_noble_calc_v1_0a0a0a0a" {
		t.Fatalf("the kernel and root disk boot: %+v", boot)
	}
	_, current := fleet.call(t, http.MethodGet, "/v1/snapshots/current?image_digest="+imageDigest+"&pool=firecracker",
		nil)
	if current["snapshot"] != digest {
		t.Fatalf("the build is what new leases use: %v", current)
	}
	_, listed := fleet.call(t, http.MethodGet, "/v1/snapshots", nil)
	if entries := listed["current"].([]any); len(entries) != 1 {
		t.Fatalf("list: %v", listed)
	}
	status, detail := fleet.call(t, http.MethodGet, "/v1/snapshots/"+digest, nil)
	if status != http.StatusOK || detail["pins"] == nil || len(detail["pins"].([]any)) != 0 {
		t.Fatalf("detail: %d %v", status, detail)
	}
	spec := detail["manifest"].(map[string]any)["image_spec"].(map[string]any)
	if spec["id"] != "noble_calc_v1" {
		t.Fatalf("the manifest keeps the image spec for reproductions: %v", spec)
	}
	entries, _ := os.ReadDir(fleet.workDir)
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".build") {
			t.Fatalf("the build's working files are gone: %s", entry.Name())
		}
	}
	if free := fleet.scheduler.FreeResources("host_a"); free.MemoryMiB != 65_536 {
		t.Fatalf("the build gives its room back: %+v", free)
	}
}

func TestBuildsRefuseImagesThisPoolCannotSnapshot(t *testing.T) {
	fleet := newSnapshotFleet(t, fakePool, true)
	for name, body := range map[string]map[string]any{
		"another vmm":       buildBody(imageBuildDir(t, "qemu_templated", "vm")),
		"a dev build":       buildBody(imageBuildDir(t, "fake", "dev")),
		"no build manifest": buildBody(t.TempDir()),
	} {
		if status, answer := fleet.call(t, http.MethodPost, "/v1/snapshots/build", body); status != 422 ||
			answer["code"] != "bad_image_build" {
			t.Fatalf("%s: %d %v", name, status, answer)
		}
	}
	tampered := imageBuildDir(t, "fake", "vm")
	if err := os.WriteFile(filepath.Join(tampered, "rootfs.ext4"), []byte("swapped disk"), 0o644); err != nil {
		t.Fatal(err)
	}
	if status, answer := fleet.call(t, http.MethodPost, "/v1/snapshots/build", buildBody(tampered)); status != 422 ||
		!strings.Contains(answer["message"].(string), "does not match its recorded sha256") {
		t.Fatalf("tampered output: %d %v", status, answer)
	}
	if status, answer := fleet.call(t, http.MethodPost, "/v1/snapshots/build",
		map[string]any{"build_dir": imageBuildDir(t, "fake", "vm")}); status != http.StatusBadRequest {
		t.Fatalf("a build names the lease size: %d %v", status, answer)
	}
	if len(fleet.driver.Boots) != 0 {
		t.Fatalf("nothing boots for a refused build: %v", fleet.driver.Boots)
	}
	upgraded := fakePool
	upgraded.VMMVersion = "fake-2"
	stale := newSnapshotFleet(t, upgraded, true)
	if status, answer := stale.call(t, http.MethodPost, "/v1/snapshots/build",
		buildBody(imageBuildDir(t, "fake", "vm"))); status != http.StatusConflict ||
		answer["code"] != "incompatible_snapshot" {
		t.Fatalf("a host whose VMM differs from its declaration: %d %v", status, answer)
	}
	if currents, _ := snapshot.ListCurrent(context.Background(), stale.store); len(currents) != 0 {
		t.Fatalf("nothing is published from a mismatched host: %v", currents)
	}
}

func TestLeasesNamingOnlyADigestPullOnceAndCountForPlacement(t *testing.T) {
	fleet := newSnapshotFleet(t, fakePool, true)
	_, built := fleet.call(t, http.MethodPost, "/v1/snapshots/build", buildBody(imageBuildDir(t, "fake", "vm")))
	digest := built["digest"].(string)
	perVM := scheduler.Resources{CPUMillicores: 2000, MemoryMiB: 2048}
	if targets := fleet.scheduler.PrefetchTargets(digest, "firecracker", perVM, 4); !slices.Equal(targets,
		[]string{"host_a"}) {
		t.Fatalf("before any lease the host would need a pull: %v", targets)
	}
	body := leaseBody(1)
	body["snapshot"] = map[string]string{"digest": digest}
	status, leased := fleet.call(t, http.MethodPost, "/v1/leases", body)
	if status != http.StatusCreated {
		t.Fatalf("lease by digest: %d %v", status, leased)
	}
	local, err := fleet.cache.Ensure(context.Background(), digest)
	if err != nil || fleet.driver.Restores[0].StatePath != local.StatePath {
		t.Fatalf("the env restores from the pulled snapshot: %+v %v", fleet.driver.Restores, err)
	}
	if targets := fleet.scheduler.PrefetchTargets(digest, "firecracker", perVM, 4); len(targets) != 0 {
		t.Fatalf("the host now holds the snapshot: %v", targets)
	}
	unknown := leaseBody(1)
	unknown["snapshot"] = map[string]string{"digest": "sha256:" + strings.Repeat("ee", 32)}
	if status, answer := fleet.call(t, http.MethodPost, "/v1/leases", unknown); status != http.StatusNotFound ||
		answer["code"] != "unknown_snapshot" {
		t.Fatalf("a lease of an unknown snapshot: %d %v", status, answer)
	}
}

func TestScheduledRunsPrefetchTheirSnapshotOntoEnoughHosts(t *testing.T) {
	fleet := newSnapshotFleet(t, fakePool, true)
	_, built := fleet.call(t, http.MethodPost, "/v1/snapshots/build", buildBody(imageBuildDir(t, "fake", "vm")))
	digest := built["digest"].(string)
	prefetch := map[string]any{"pool": "firecracker", "envs": 8, "memory_mib": 2048, "cpu_millicores": 2000}
	status, answer := fleet.call(t, http.MethodPost, "/v1/snapshots/"+digest+"/prefetch", prefetch)
	if status != http.StatusAccepted || len(answer["hosts"].([]any)) != 1 {
		t.Fatalf("prefetch: %d %v", status, answer)
	}
	fleet.service.WaitForPrefetches()
	if !fleet.cache.Has(digest) {
		t.Fatal("the host pulled the snapshot before any lease")
	}
	if _, again := fleet.call(t, http.MethodPost, "/v1/snapshots/"+digest+"/prefetch", prefetch); len(
		again["hosts"].([]any)) != 0 {
		t.Fatalf("hosts that hold the snapshot need no second pull: %v", again)
	}
	missing := "sha256:" + strings.Repeat("ee", 32)
	if status, _ := fleet.call(t, http.MethodPost, "/v1/snapshots/"+missing+"/prefetch", prefetch); status != 404 {
		t.Fatalf("prefetching an unknown snapshot: %d", status)
	}
	if status, _ := fleet.call(t, http.MethodPost, "/v1/snapshots/"+digest+"/prefetch",
		map[string]any{"pool": "firecracker"}); status != http.StatusBadRequest {
		t.Fatalf("a prefetch names the run's size: %d", status)
	}
}

func TestPinsKeepSnapshotsThroughCollection(t *testing.T) {
	fleet := newSnapshotFleet(t, fakePool, true)
	_, built := fleet.call(t, http.MethodPost, "/v1/snapshots/build", buildBody(imageBuildDir(t, "fake", "vm")))
	digest := built["digest"].(string)
	status, pin := fleet.call(t, http.MethodPut, "/v1/snapshots/"+digest+"/pins/run:experiment_000001_run_000", nil)
	if status != http.StatusOK || pin["holder"] != "run:experiment_000001_run_000" {
		t.Fatalf("pin: %d %v", status, pin)
	}
	_, detail := fleet.call(t, http.MethodGet, "/v1/snapshots/"+digest, nil)
	if pins := detail["pins"].([]any); len(pins) != 1 {
		t.Fatalf("detail pins: %v", detail)
	}
	status, report := fleet.call(t, http.MethodPost, "/v1/snapshots/collect", nil)
	if status != http.StatusOK || len(report["removed"].([]any)) != 0 {
		t.Fatalf("collect: %d %v", status, report)
	}
	request, _ := http.NewRequest(http.MethodDelete, fleet.server.URL+"/v1/snapshots/"+digest+
		"/pins/run:experiment_000001_run_000", nil)
	response, err := http.DefaultClient.Do(request)
	if err != nil || response.StatusCode != http.StatusNoContent {
		t.Fatalf("unpin: %v %v", response, err)
	}
	if status, _ := fleet.call(t, http.MethodPut, "/v1/snapshots/sha256:short/pins/run:x", nil); status != 400 {
		t.Fatalf("a malformed digest: %d", status)
	}
}

func TestFleetdWithoutASnapshotStoreSaysSo(t *testing.T) {
	fleet := newSnapshotFleet(t, fakePool, false)
	for _, call := range []struct{ method, path string }{
		{http.MethodGet, "/v1/snapshots"},
		{http.MethodPost, "/v1/snapshots/build"},
		{http.MethodPost, "/v1/snapshots/collect"},
		{http.MethodGet, "/v1/snapshots/" + imageDigest},
		{http.MethodPut, "/v1/snapshots/" + imageDigest + "/pins/run:x"},
	} {
		body := any(nil)
		if call.path == "/v1/snapshots/build" {
			body = buildBody(t.TempDir())
		}
		if status, answer := fleet.call(t, call.method, call.path, body); status != http.StatusServiceUnavailable ||
			answer["code"] != "no_snapshot_store" {
			t.Fatalf("%s %s: %d %v", call.method, call.path, status, answer)
		}
	}
	if err := fleet.service.MarkCachedSnapshots(); err != nil {
		t.Fatal(err)
	}
}
