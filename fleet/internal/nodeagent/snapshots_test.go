package nodeagent

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/clock"
	"rlworld/fleet/internal/snapshot"
	"rlworld/fleet/internal/vmm"
	"rlworld/fleet/internal/vmm/fake"
)

var testPool = snapshot.Compatibility{Pool: "linux", VMM: "fake", VMMVersion: "fake-1", CPUModel: "fleet_baseline"}

const (
	calcWindow = `{"accessibility": [{"role": "application", "name": "soffice"},` +
		` {"role": "frame", "name": "Untitled 1 - LibreOffice Calc"}]}`
	splashWindow = `{"accessibility": [{"role": "frame", "name": "LibreOffice"}]}`
)

// heldStore holds back reads of the keys it names until released.
type heldStore struct {
	snapshot.ObjectStore
	held    map[string]bool
	release chan struct{}
	mu      sync.Mutex
	gets    int
}

func (store *heldStore) Get(ctx context.Context, key string) (io.ReadCloser, error) {
	store.mu.Lock()
	store.gets++
	store.mu.Unlock()
	if store.held[key] {
		<-store.release
	}
	return store.ObjectStore.Get(ctx, key)
}

func (store *heldStore) getCount() int {
	store.mu.Lock()
	defer store.mu.Unlock()
	return store.gets
}

func publishedSnapshot(t *testing.T) (snapshot.ObjectStore, snapshot.Manifest) {
	t.Helper()
	store := snapshot.DirStore{Root: t.TempDir()}
	source := t.TempDir()
	paths := map[string]string{}
	for name, content := range map[string]string{snapshot.StateFile: "device state",
		snapshot.MemoryFile: strings.Repeat("guest memory ", 40), snapshot.DiskFile: "root disk"} {
		paths[name] = filepath.Join(source, name)
		if err := os.WriteFile(paths[name], []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	draft := snapshot.Manifest{Image: "noble_calc_v1", ImageDigest: "sha256:" + strings.Repeat("ab", 32),
		Compatibility: testPool, MemoryMiB: 4096, VCPUs: 4}
	manifest, err := snapshot.Publisher{Store: store, ChunkSize: 64}.Publish(context.Background(), draft, paths)
	if err != nil {
		t.Fatal(err)
	}
	return store, manifest
}

func containsFragment(commands []string, fragment string) bool {
	for _, command := range commands {
		if strings.Contains(command, fragment) {
			return true
		}
	}
	return false
}

// lazyDriver is the fake driver standing in for one that serves memory pages while they are pulled.
type lazyDriver struct {
	*fake.Driver
}

func (lazyDriver) RestoresLazily() bool {
	return true
}

// restoreOnly is a driver that cannot cold boot.
type restoreOnly struct {
	vmm.Driver
}

func newSnapshotAgent(t *testing.T, driver vmm.Driver, cache *snapshot.Cache, lazy bool,
	guest GuestChannel) (*Agent, *recordingCommands) {
	t.Helper()
	commands := &recordingCommands{}
	fakeClock := clock.NewFake(time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC))
	agent := New(Config{HostID: "host_a", OverlayDir: t.TempDir(), Snapshots: cache, LazyMemory: lazy}, driver,
		guest, commands, fakeClock, fakeClock.Advance)
	return agent, commands
}

func TestLeasesNamingADigestPullTheSnapshotOnce(t *testing.T) {
	ctx := context.Background()
	inner, manifest := publishedSnapshot(t)
	store := &heldStore{ObjectStore: inner, held: map[string]bool{}}
	cache := snapshot.NewCache(store, t.TempDir(), testPool, 2)
	driver := fake.New()
	agent, commands := newSnapshotAgent(t, driver, cache, false, &scriptedGuest{hashes: []string{"a"}})
	request := StartRequest{EnvID: "env_1", SnapshotDigest: manifest.Digest, MemoryMiB: 512, VCPUs: 1}
	if _, err := agent.Start(ctx, request); err != nil {
		t.Fatal(err)
	}
	local, err := cache.Ensure(ctx, manifest.Digest)
	if err != nil {
		t.Fatal(err)
	}
	restored := driver.Restores[0]
	if restored.StatePath != local.StatePath || restored.MemoryPath != local.MemoryPath ||
		restored.LazyMemory != nil {
		t.Fatalf("the restore uses the pulled files: %+v", restored)
	}
	if restored.MemoryMiB != 4096 || restored.VCPUs != 4 {
		t.Fatalf("a restore takes the memory and vCPUs the snapshot was taken with: %+v", restored)
	}
	if !containsFragment(commands.commands, "cp --reflink=always "+local.DiskPath) {
		t.Fatalf("the overlay copies the pulled disk: %v", commands.commands)
	}
	pulled := store.getCount()
	request.EnvID = "env_2"
	if _, err := agent.Start(ctx, request); err != nil || store.getCount() != pulled {
		t.Fatalf("the second lease finds the snapshot on the host: %v, %d more reads", err, store.getCount()-pulled)
	}
	cached, err := agent.CachedSnapshots()
	if err != nil || !slices.Equal(cached, []string{manifest.Digest}) {
		t.Fatalf("cached: %v %v", cached, err)
	}
	named := startRequest("env_3")
	named.SnapshotDigest = manifest.Digest
	if _, err := agent.Start(ctx, named); err != nil || driver.Restores[2].StatePath != "/snapshots/base.state" {
		t.Fatalf("a lease that names files skips the store: %v %+v", err, driver.Restores[2])
	}
}

func TestDigestLeasesNeedAStoreAndAPullableSnapshot(t *testing.T) {
	ctx := context.Background()
	agent, _ := newSnapshotAgent(t, fake.New(), nil, false, &scriptedGuest{hashes: []string{"a"}})
	request := StartRequest{EnvID: "env_1", SnapshotDigest: "sha256:" + strings.Repeat("cd", 32)}
	if _, err := agent.Start(ctx, request); !errors.Is(err, ErrNoSnapshotStore) {
		t.Fatalf("no store: %v", err)
	}
	if err := agent.Prefetch(ctx, request.SnapshotDigest); !errors.Is(err, ErrNoSnapshotStore) {
		t.Fatalf("prefetch without a store: %v", err)
	}
	if cached, err := agent.CachedSnapshots(); cached != nil || err != nil {
		t.Fatalf("nothing is cached without a store: %v %v", cached, err)
	}
	store, _ := publishedSnapshot(t)
	driver := fake.New()
	withStore, _ := newSnapshotAgent(t, driver, snapshot.NewCache(store, t.TempDir(), testPool, 2), false,
		&scriptedGuest{hashes: []string{"a"}})
	if _, err := withStore.Start(ctx, request); !errors.Is(err, snapshot.ErrNotFound) ||
		!strings.Contains(err.Error(), "pull snapshot") || len(driver.Restores) != 0 {
		t.Fatalf("an unknown snapshot: %v", err)
	}
	if envs := withStore.Envs(); len(envs) != 0 {
		t.Fatalf("a failed pull starts nothing: %v", envs)
	}
}

func TestLazyRestoresStartBeforeTheMemoryArrives(t *testing.T) {
	ctx := context.Background()
	inner, manifest := publishedSnapshot(t)
	held := map[string]bool{}
	for _, chunk := range manifest.Files[snapshot.MemoryFile].Chunks {
		held["chunks/"+strings.TrimPrefix(chunk.Digest, "sha256:")] = true
	}
	store := &heldStore{ObjectStore: inner, held: held, release: make(chan struct{})}
	cache := snapshot.NewCache(store, t.TempDir(), testPool, 2)
	driver := lazyDriver{fake.New()}
	agent, _ := newSnapshotAgent(t, driver, cache, true, &scriptedGuest{hashes: []string{"a"}})
	if _, err := agent.Start(ctx, StartRequest{EnvID: "env_1", SnapshotDigest: manifest.Digest}); err != nil {
		t.Fatal(err)
	}
	restored := driver.Restores[0]
	if restored.LazyMemory == nil || restored.LazyMemory.Size() != manifest.Files[snapshot.MemoryFile].Size {
		t.Fatalf("the restore serves memory lazily: %+v", restored)
	}
	if cache.Has(manifest.Digest) {
		t.Fatal("the memory is still held back")
	}
	close(store.release)
	page := make([]byte, 12)
	if read, err := restored.LazyMemory.ReadAt(page, 13); err != nil || read != 12 || string(page) != "guest memory" {
		t.Fatalf("a page read while pulling: %q %v", page, err)
	}
	if err := agent.Destroy(ctx, "env_1"); err != nil {
		t.Fatal(err)
	}
	if _, err := restored.LazyMemory.ReadAt(page, 0); err == nil {
		t.Fatal("destroying the env releases its memory handle")
	}
	if err := agent.Prefetch(ctx, manifest.Digest); err != nil || !cache.Has(manifest.Digest) {
		t.Fatalf("prefetch finishes the pull: %v", err)
	}
	if _, err := agent.Start(ctx, StartRequest{EnvID: "env_2", SnapshotDigest: manifest.Digest}); err != nil {
		t.Fatal(err)
	}
	if complete := driver.Restores[1]; complete.LazyMemory != nil || complete.MemoryPath == "" {
		t.Fatalf("once the memory is local the restore maps the file: %+v", complete)
	}
	plain := fake.New()
	eager, _ := newSnapshotAgent(t, plain, snapshot.NewCache(inner, t.TempDir(), testPool, 2), true,
		&scriptedGuest{hashes: []string{"a"}})
	if _, err := eager.Start(ctx, StartRequest{EnvID: "env_3", SnapshotDigest: manifest.Digest}); err != nil ||
		plain.Restores[0].LazyMemory != nil {
		t.Fatalf("a driver that cannot serve pages waits for the whole pull: %v", err)
	}
}

// bootingGuest is a guest coming up: its agent answers after a few polls, the probe window opens later and the
// screen settles last.
type bootingGuest struct {
	mu            sync.Mutex
	unreachable   int
	observations  []string
	hashes        []string
	observeCalls  int
	hashCalls     int
	observedKinds [][]string
}

func (guest *bootingGuest) Observe(_ context.Context, _ string, kinds []string) (json.RawMessage, error) {
	guest.mu.Lock()
	defer guest.mu.Unlock()
	guest.observedKinds = append(guest.observedKinds, kinds)
	if guest.unreachable > 0 {
		guest.unreachable--
		return nil, errors.New("vsock connection refused")
	}
	observation := guest.observations[min(guest.observeCalls, len(guest.observations)-1)]
	guest.observeCalls++
	return json.RawMessage(observation), nil
}

func (guest *bootingGuest) ScreenHash(context.Context, string) (string, error) {
	guest.mu.Lock()
	defer guest.mu.Unlock()
	hash := guest.hashes[min(guest.hashCalls, len(guest.hashes)-1)]
	guest.hashCalls++
	return hash, nil
}

func (guest *bootingGuest) Prepare(context.Context, string, time.Time, []byte) error { return nil }
func (guest *bootingGuest) Act(context.Context, string, json.RawMessage) error       { return nil }
func (guest *bootingGuest) Inject(context.Context, string, json.RawMessage) error    { return nil }
func (guest *bootingGuest) Extract(context.Context, string, []string) (json.RawMessage, error) {
	return nil, nil
}
func (guest *bootingGuest) DialVNC(context.Context, string) (net.Conn, *bufio.Reader, error) {
	return nil, nil, errors.New("no screen during builds")
}

// alternatingHashes is a screen that keeps changing, such as a clock or a spinner.
func alternatingHashes(count int) []string {
	hashes := make([]string, count)
	for index := range hashes {
		hashes[index] = []string{"a", "b"}[index%2]
	}
	return hashes
}

func buildRequest(outputDir string) BuildRequest {
	return BuildRequest{BuildID: "build_noble_calc_v1", KernelPath: "/builds/noble/vmlinux",
		DiskPath: "/builds/noble/rootfs.ext4", MemoryMiB: 4096, VCPUs: 4, ReadyWindow: "Calc",
		StableFor: 500 * time.Millisecond, Timeout: 3 * time.Minute, OutputDir: outputDir}
}

func TestTheBuilderSnapshotsOnceTheProbeWindowIsUpAndTheScreenIsStill(t *testing.T) {
	ctx := context.Background()
	driver := fake.New()
	driver.WritesSnapshots = true
	guest := &bootingGuest{unreachable: 3, observations: []string{splashWindow, splashWindow, calcWindow},
		hashes: []string{"loading", "drawing", "ready", "ready", "ready", "ready", "ready", "ready", "ready"}}
	agent, commands := newSnapshotAgent(t, driver, nil, false, guest)
	outputDir := filepath.Join(t.TempDir(), ".build-noble")
	result, err := agent.BuildSnapshot(ctx, buildRequest(outputDir))
	if err != nil {
		t.Fatal(err)
	}
	if result.StatePath != filepath.Join(outputDir, "state") || result.DiskPath != filepath.Join(outputDir, "disk") ||
		result.MemoryPath != filepath.Join(outputDir, "memory") || result.VMMVersion != "fake-1" {
		t.Fatalf("result: %+v", result)
	}
	if state, _ := os.ReadFile(result.StatePath); string(state) != "full state of build_noble_calc_v1" {
		t.Fatalf("the snapshot is full: %q", state)
	}
	// Three refused observes, two without the window, one with it, then hashes until "ready" holds for 500 ms.
	if result.ReadyAfter != 1300*time.Millisecond {
		t.Fatalf("ready after %s", result.ReadyAfter)
	}
	boot := driver.Boots[0]
	if boot.DiskPath != result.DiskPath || boot.KernelPath != "/builds/noble/vmlinux" || boot.MemoryMiB != 4096 ||
		boot.MemoryPath != result.MemoryPath || boot.Namespace == "" {
		t.Fatalf("the build boots a copy of the image's disk in a clone slot: %+v", boot)
	}
	if !containsFragment(commands.commands, "cp --reflink=auto /builds/noble/rootfs.ext4 "+result.DiskPath) ||
		!containsFragment(commands.commands, "ip netns add "+boot.Namespace) ||
		!containsFragment(commands.commands, "ip netns del "+boot.Namespace) {
		t.Fatalf("the build copies the disk and tears its network down: %v", commands.commands)
	}
	wantOperations := []string{"boot build_noble_calc_v1", "version build_noble_calc_v1",
		"snapshot build_noble_calc_v1", "destroy build_noble_calc_v1"}
	if !slices.Equal(driver.Operations, wantOperations) {
		t.Fatalf("operations %v", driver.Operations)
	}
	if !slices.Equal(guest.observedKinds[0], []string{"accessibility_tree"}) {
		t.Fatalf("the probe reads only the accessibility tree: %v", guest.observedKinds[0])
	}
	if len(agent.usedIndexes) != 0 {
		t.Fatalf("the build gives its clone slot back: %v", agent.usedIndexes)
	}
}

func TestABuildThatNeverGetsReadyFailsAndCleansUp(t *testing.T) {
	ctx := context.Background()
	driver := fake.New()
	guest := &bootingGuest{observations: []string{splashWindow}, hashes: []string{"a"}}
	agent, commands := newSnapshotAgent(t, driver, nil, false, guest)
	request := buildRequest(filepath.Join(t.TempDir(), ".build"))
	request.Timeout = 2 * time.Second
	if _, err := agent.BuildSnapshot(ctx, request); err == nil ||
		!strings.Contains(err.Error(), "still waiting for the Calc window") {
		t.Fatalf("a probe window that never opens: %v", err)
	}
	if !slices.Contains(driver.Operations, "destroy build_noble_calc_v1") ||
		!containsFragment(commands.commands, "ip netns del") || len(agent.usedIndexes) != 0 {
		t.Fatalf("a failed build destroys its VM and network: %v %v", driver.Operations, commands.commands)
	}
	flicker := &bootingGuest{observations: []string{calcWindow}, hashes: alternatingHashes(40)}
	flickering, _ := newSnapshotAgent(t, fake.New(), nil, false, flicker)
	if _, err := flickering.BuildSnapshot(ctx, request); err == nil ||
		!strings.Contains(err.Error(), "the screen to hold still") {
		t.Fatalf("a screen that never settles: %v", err)
	}
	unreachable := &bootingGuest{unreachable: 1 << 30, observations: []string{calcWindow}, hashes: []string{"a"}}
	silent, _ := newSnapshotAgent(t, fake.New(), nil, false, unreachable)
	if _, err := silent.BuildSnapshot(ctx, request); err == nil || !strings.Contains(err.Error(), "the guest agent") {
		t.Fatalf("a guest agent that never answers: %v", err)
	}
	failing := fake.New()
	failing.FailNext["boot"] = errors.New("kvm unavailable")
	broken, brokenCommands := newSnapshotAgent(t, failing, nil, false, guest)
	if _, err := broken.BuildSnapshot(ctx, request); err == nil || !strings.Contains(err.Error(), "kvm unavailable") {
		t.Fatalf("a failed boot: %v", err)
	}
	if !containsFragment(brokenCommands.commands, "ip netns del") || len(broken.usedIndexes) != 0 {
		t.Fatalf("a failed boot still frees the network: %v", brokenCommands.commands)
	}
	noBoot, _ := newSnapshotAgent(t, restoreOnly{fake.New()}, nil, false, guest)
	if _, err := noBoot.BuildSnapshot(ctx, request); err == nil || !strings.Contains(err.Error(), "cannot cold boot") {
		t.Fatalf("a driver without cold boot: %v", err)
	}
}

func TestBuildsWithoutAProbeWindowWaitForAStillScreen(t *testing.T) {
	driver := fake.New()
	driver.WritesSnapshots = true
	guest := &bootingGuest{observations: []string{splashWindow}, hashes: []string{"x", "x", "x", "x", "x", "x"}}
	agent, _ := newSnapshotAgent(t, driver, nil, false, guest)
	request := buildRequest(filepath.Join(t.TempDir(), ".build"))
	request.ReadyWindow = ""
	result, err := agent.BuildSnapshot(context.Background(), request)
	if err != nil || result.ReadyAfter != 500*time.Millisecond || len(guest.observedKinds) != 0 {
		t.Fatalf("no window to wait for: %+v %v", result, err)
	}
}

func TestProbeWindowsMatchTopLevelWindowTitles(t *testing.T) {
	for observation, want := range map[string]bool{
		calcWindow:   true,
		splashWindow: false,
		`{"accessibility": [{"role": "push button", "name": "Calc"}]}`:     false,
		`{"accessibility": [{"role": "window", "name": "Book1 - Excel"}]}`: false,
		`not json`: false,
	} {
		if hasWindow(json.RawMessage(observation), "Calc") != want {
			t.Fatalf("%s: want %v", observation, want)
		}
	}
	if !hasWindow(json.RawMessage(`{"accessibility": [{"role": "window", "name": "Book1 - Excel"}]}`), "Excel") {
		t.Fatal("UIA windows count too")
	}
}

// memoryInStateDriver is a driver whose snapshots keep memory in the state file, as a QEMU clone's do.
type memoryInStateDriver struct {
	*fake.Driver
}

func (driver memoryInStateDriver) Snapshot(ctx context.Context, vmID string, kind vmm.SnapshotKind, statePath,
	memoryPath string) (vmm.SnapshotRef, error) {
	written, err := driver.Driver.Snapshot(ctx, vmID, kind, statePath, memoryPath)
	written.MemoryPath = ""
	return written, err
}

func TestARestoredEnvIsCapturedWithItsDisk(t *testing.T) {
	ctx := context.Background()
	driver := fake.New()
	driver.WritesSnapshots = true
	agent, commands := newSnapshotAgent(t, driver, nil, false, &scriptedGuest{hashes: []string{"a"}})
	request := startRequest("env_1")
	request.Resume = true
	if _, err := agent.Start(ctx, request); err != nil {
		t.Fatal(err)
	}
	outputDir := filepath.Join(t.TempDir(), ".world-env_1")
	captured, err := agent.CaptureSnapshot(ctx, "env_1", outputDir)
	if err != nil {
		t.Fatal(err)
	}
	if state, _ := os.ReadFile(captured.StatePath); string(state) != "full state of env_1" {
		t.Fatalf("captured state: %q", state)
	}
	overlay := agent.envs["env_1"].overlayPath
	if !containsFragment(commands.commands, "cp --reflink=auto "+overlay+" "+captured.DiskPath) {
		t.Fatalf("the disk captured with the memory is the env's overlay: %v", commands.commands)
	}
	if agent.envs["env_1"].state != vmm.Paused || captured.VMMVersion != "fake-1" {
		t.Fatalf("a captured env is paused: %v %+v", agent.envs["env_1"].state, captured)
	}
	if _, err := agent.CaptureSnapshot(ctx, "missing", outputDir); !errors.Is(err, ErrUnknownEnv) {
		t.Fatalf("unknown env: %v", err)
	}
	inState, _ := newSnapshotAgent(t, memoryInStateDriver{fake.New()}, nil, false,
		&scriptedGuest{hashes: []string{"a"}})
	if _, err := inState.Start(ctx, startRequest("env_2")); err != nil {
		t.Fatal(err)
	}
	if _, err := inState.CaptureSnapshot(ctx, "env_2", outputDir); err == nil ||
		!strings.Contains(err.Error(), "memory into its state") {
		t.Fatalf("a snapshot without its own memory file: %v", err)
	}
	docker := fake.New()
	docker.DriverName = "docker"
	containers, _ := newSnapshotAgent(t, docker, nil, false, &scriptedGuest{hashes: []string{"a"}})
	if _, err := containers.Start(ctx, startRequest("env_3")); err != nil {
		t.Fatal(err)
	}
	if _, err := containers.CaptureSnapshot(ctx, "env_3", outputDir); err == nil {
		t.Fatal("containers keep no snapshot files")
	}
}
