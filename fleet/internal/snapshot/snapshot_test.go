package snapshot

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const testChunkSize = 8

var (
	imageDigest = "sha256:" + strings.Repeat("ab", 32)
	builtAt     = time.Date(2026, 10, 3, 9, 0, 0, 0, time.UTC)
	linuxPool   = Compatibility{Pool: "linux", VMM: "firecracker", VMMVersion: "1.12.0", CPUModel: "fleet_baseline"}
)

// countingStore counts reads and can hold them until released, to observe how pulls share work.
type countingStore struct {
	ObjectStore
	mu      sync.Mutex
	gets    map[string]int
	release chan struct{}
}

func newCountingStore(inner ObjectStore) *countingStore {
	return &countingStore{ObjectStore: inner, gets: map[string]int{}}
}

func (store *countingStore) Get(ctx context.Context, key string) (io.ReadCloser, error) {
	store.mu.Lock()
	store.gets[key]++
	release := store.release
	store.mu.Unlock()
	if release != nil && strings.HasPrefix(key, "chunks/") {
		<-release
	}
	return store.ObjectStore.Get(ctx, key)
}

func (store *countingStore) chunkGets() int {
	store.mu.Lock()
	defer store.mu.Unlock()
	total := 0
	for key, count := range store.gets {
		if strings.HasPrefix(key, "chunks/") {
			total += count
		}
	}
	return total
}

func fixedNow() time.Time {
	return builtAt
}

func writeFiles(t *testing.T, dir string, contents map[string][]byte) map[string]string {
	t.Helper()
	paths := map[string]string{}
	for name, content := range contents {
		paths[name] = filepath.Join(dir, name)
		if err := os.WriteFile(paths[name], content, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return paths
}

func draft() Manifest {
	return Manifest{Image: "noble_calc_v1", ImageDigest: imageDigest,
		ImageSpec: json.RawMessage(`{"id":"noble_calc_v1"}`), Compatibility: linuxPool, MemoryMiB: 2048, VCPUs: 2}
}

func snapshotContents(memory string) map[string][]byte {
	return map[string][]byte{
		StateFile:  []byte("device state v1"),
		MemoryFile: append([]byte(memory), make([]byte, 3*testChunkSize)...),
		DiskFile:   []byte("ext4 superblock and the apps"),
	}
}

func publish(t *testing.T, store ObjectStore, contents map[string][]byte) Manifest {
	t.Helper()
	return publishAt(t, store, contents, fixedNow)
}

func publishAt(t *testing.T, store ObjectStore, contents map[string][]byte, now func() time.Time) Manifest {
	t.Helper()
	publisher := Publisher{Store: store, ChunkSize: testChunkSize, Workers: 3, Now: now}
	manifest, err := publisher.Publish(context.Background(), draft(), writeFiles(t, t.TempDir(), contents))
	if err != nil {
		t.Fatal(err)
	}
	return manifest
}

func eightDaysLater() time.Time {
	return builtAt.Add(8 * 24 * time.Hour)
}

func chunkCount(t *testing.T, store ObjectStore) int {
	t.Helper()
	keys, err := store.List(context.Background(), "chunks/")
	if err != nil {
		t.Fatal(err)
	}
	return len(keys)
}

func digestOf(data []byte) string {
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

func TestDirStoreWritesWholeObjectsUnderCleanKeys(t *testing.T) {
	ctx := context.Background()
	store := DirStore{Root: t.TempDir()}
	for _, key := range []string{"chunks/b", "chunks/a", "pins/x/1.json", "snapshots/c.json"} {
		if err := store.Put(ctx, key, strings.NewReader(key)); err != nil {
			t.Fatal(err)
		}
	}
	keys, err := store.List(ctx, "chunks/")
	if err != nil || strings.Join(keys, ",") != "chunks/a,chunks/b" {
		t.Fatalf("list: %v %v", keys, err)
	}
	if all, _ := store.List(ctx, ""); len(all) != 4 {
		t.Fatalf("everything: %v", all)
	}
	if missing, err := store.List(ctx, "trash/"); err != nil || len(missing) != 0 {
		t.Fatalf("an absent prefix lists nothing: %v %v", missing, err)
	}
	raw, err := readAll(ctx, store, "pins/x/1.json")
	if err != nil || string(raw) != "pins/x/1.json" {
		t.Fatalf("get: %q %v", raw, err)
	}
	if _, err := store.Get(ctx, "chunks/zzz"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing object: %v", err)
	}
	if found, err := store.Exists(ctx, "chunks/a"); !found || err != nil {
		t.Fatalf("exists: %v %v", found, err)
	}
	if err := store.Delete(ctx, "chunks/a"); err != nil {
		t.Fatal(err)
	}
	if err := store.Delete(ctx, "chunks/a"); err != nil {
		t.Fatalf("deleting twice: %v", err)
	}
	if found, _ := store.Exists(ctx, "chunks/a"); found {
		t.Fatal("deleted object still exists")
	}
	for _, key := range []string{"", "/etc/passwd", "../outside", "chunks/../../outside", "chunks/.partial-x"} {
		if err := store.Put(ctx, key, strings.NewReader("x")); err == nil {
			t.Fatalf("key %q must be refused", key)
		}
	}
	canceled, cancel := context.WithCancel(ctx)
	cancel()
	if err := store.Put(canceled, "chunks/late", strings.NewReader("never")); err == nil {
		t.Fatal("a canceled put must fail")
	}
	entries, _ := os.ReadDir(filepath.Join(store.Root, "chunks"))
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), temporaryPrefix) {
			t.Fatalf("a failed put left %s behind", entry.Name())
		}
	}
}

func TestPublishingChunksFilesOnceAndNamesSnapshotsByContent(t *testing.T) {
	store := DirStore{Root: t.TempDir()}
	manifest := publish(t, store, snapshotContents("memory pages"))
	memory := manifest.Files[MemoryFile]
	if memory.Size != int64(len("memory pages")+3*testChunkSize) || len(memory.Chunks) != 5 {
		t.Fatalf("memory chunks: %+v", memory)
	}
	if last := memory.Chunks[len(memory.Chunks)-1]; last.Size != 4 {
		t.Fatalf("the last chunk keeps the remainder: %+v", last)
	}
	if manifest.Digest == "" || !manifest.BuiltAt.Equal(builtAt) || manifest.Bytes() != 15+36+28 {
		t.Fatalf("manifest: %+v", manifest)
	}
	digest, err := manifest.ContentDigest()
	if err != nil || digest != manifest.Digest {
		t.Fatalf("content digest: %s %v", digest, err)
	}
	later := manifest
	later.BuiltAt = builtAt.Add(time.Hour)
	if laterDigest, _ := later.ContentDigest(); laterDigest != manifest.Digest {
		t.Fatal("when a snapshot was built is not part of what it is")
	}
	firstChunks := chunkCount(t, store)
	again := publish(t, store, snapshotContents("memory pages"))
	if again.Digest != manifest.Digest || chunkCount(t, store) != firstChunks {
		t.Fatalf("the same files publish to the same digest and add no chunks: %s", again.Digest)
	}
	changed := publish(t, store, snapshotContents("memory PAGES"))
	if changed.Digest == manifest.Digest || chunkCount(t, store) != firstChunks+2 {
		t.Fatalf("a changed page adds only its chunks: %d", chunkCount(t, store)-firstChunks)
	}
	loaded, err := LoadManifest(context.Background(), store, changed.Digest)
	if err != nil || loaded.Digest != changed.Digest || string(loaded.ImageSpec) != `{"id":"noble_calc_v1"}` {
		t.Fatalf("load: %+v %v", loaded, err)
	}
	listed, err := ListManifests(context.Background(), store)
	if err != nil || len(listed) != 2 {
		t.Fatalf("list: %d %v", len(listed), err)
	}
}

func TestPublishingRefusesIncompleteOrMislabelledSnapshots(t *testing.T) {
	store := DirStore{Root: t.TempDir()}
	publisher := Publisher{Store: store, ChunkSize: testChunkSize, Now: fixedNow}
	paths := writeFiles(t, t.TempDir(), snapshotContents("pages"))
	delete(paths, DiskFile)
	if _, err := publisher.Publish(context.Background(), draft(), paths); err == nil ||
		!strings.Contains(err.Error(), "no disk file") {
		t.Fatalf("missing disk: %v", err)
	}
	badImage := draft()
	badImage.ImageDigest = "sha1:abc"
	if _, err := publisher.Publish(context.Background(), badImage, paths); !errors.Is(err, ErrBadDigest) {
		t.Fatalf("bad image digest: %v", err)
	}
	badSpec := draft()
	badSpec.ImageSpec = json.RawMessage(`{"id":`)
	if _, err := publisher.Publish(context.Background(), badSpec, paths); err == nil {
		t.Fatal("an image spec that is not JSON must be refused")
	}
	paths[DiskFile] = filepath.Join(t.TempDir(), "missing.ext4")
	if _, err := publisher.Publish(context.Background(), draft(), paths); err == nil {
		t.Fatal("an unreadable file must fail the publish")
	}
	if manifests, _ := ListManifests(context.Background(), store); len(manifests) != 0 {
		t.Fatalf("failed publishes write no manifest: %d", len(manifests))
	}
}

func TestTamperedManifestsAreRefused(t *testing.T) {
	ctx := context.Background()
	store := DirStore{Root: t.TempDir()}
	manifest := publish(t, store, snapshotContents("pages"))
	key, _ := manifestKey(manifest.Digest)
	tampered := manifest
	tampered.MemoryMiB = 4096
	encoded, _ := json.Marshal(tampered)
	if err := store.Put(ctx, key, bytes.NewReader(encoded)); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadManifest(ctx, store, manifest.Digest); err == nil ||
		!strings.Contains(err.Error(), "does not match its content") {
		t.Fatalf("tampered manifest: %v", err)
	}
	other := publish(t, store, snapshotContents("other pages"))
	otherKey, _ := manifestKey(other.Digest)
	raw, _ := readAll(ctx, store, otherKey)
	if err := store.Put(ctx, key, bytes.NewReader(raw)); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadManifest(ctx, store, manifest.Digest); err == nil || !strings.Contains(err.Error(), "names") {
		t.Fatalf("a manifest stored under another digest: %v", err)
	}
	short := other
	short.Files = map[string]File{StateFile: other.Files[StateFile], MemoryFile: other.Files[MemoryFile]}
	if err := short.validate(); err == nil {
		t.Fatal("a manifest without its disk is invalid")
	}
	if _, err := LoadManifest(ctx, store, "sha256:short"); !errors.Is(err, ErrBadDigest) {
		t.Fatalf("bad digest: %v", err)
	}
}

func TestTheIndexNamesTheCurrentSnapshotPerImageAndPool(t *testing.T) {
	ctx := context.Background()
	store := DirStore{Root: t.TempDir()}
	first := publish(t, store, snapshotContents("pages"))
	if _, err := CurrentSnapshot(ctx, store, imageDigest, "linux"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("nothing is current before SetCurrent: %v", err)
	}
	if _, err := SetCurrent(ctx, store, first, builtAt); err != nil {
		t.Fatal(err)
	}
	rebuilt := publish(t, store, snapshotContents("pages after a vmm upgrade"))
	current, err := SetCurrent(ctx, store, rebuilt, builtAt.Add(time.Hour))
	if err != nil || current.Snapshot != rebuilt.Digest {
		t.Fatalf("set current: %+v %v", current, err)
	}
	found, err := CurrentSnapshot(ctx, store, imageDigest, "linux")
	if err != nil || found.Snapshot != rebuilt.Digest || found.Image != "noble_calc_v1" ||
		!found.UpdatedAt.Equal(builtAt.Add(time.Hour)) {
		t.Fatalf("current: %+v %v", found, err)
	}
	listed, err := ListCurrent(ctx, store)
	if err != nil || len(listed) != 1 || listed[0].Pool != "linux" {
		t.Fatalf("list current: %+v %v", listed, err)
	}
	for _, pool := range []string{"", "Linux", "../linux", "linux/dev"} {
		if _, err := CurrentSnapshot(ctx, store, imageDigest, pool); err == nil || errors.Is(err, ErrNotFound) {
			t.Fatalf("pool %q must be refused: %v", pool, err)
		}
	}
}

func TestTheCachePullsEachSnapshotOnceAndChecksEveryChunk(t *testing.T) {
	ctx := context.Background()
	inner := DirStore{Root: t.TempDir()}
	contents := snapshotContents("pages")
	manifest := publish(t, inner, contents)
	store := newCountingStore(inner)
	store.release = make(chan struct{})
	cache := NewCache(store, t.TempDir(), linuxPool, 4)
	results := make(chan error, 3)
	waiters := &ensureWaiters{cache: cache, digest: manifest.Digest, results: results}
	for range 3 {
		go waiters.ensure()
	}
	time.Sleep(20 * time.Millisecond)
	close(store.release)
	for range 3 {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
	local, err := cache.Ensure(ctx, manifest.Digest)
	if err != nil {
		t.Fatal(err)
	}
	for name, path := range map[string]string{StateFile: local.StatePath, MemoryFile: local.MemoryPath,
		DiskFile: local.DiskPath} {
		got, err := os.ReadFile(path)
		if err != nil || !bytes.Equal(got, contents[name]) {
			t.Fatalf("%s differs after the pull: %q %v", name, got, err)
		}
	}
	zeroChunks := 0
	for _, chunk := range manifest.Files[MemoryFile].Chunks {
		if chunk.Digest == digestOf(make([]byte, chunk.Size)) {
			zeroChunks++
		}
	}
	if zeroChunks != 3 || store.chunkGets() != len(manifest.Files[StateFile].Chunks)+
		len(manifest.Files[MemoryFile].Chunks)-zeroChunks+len(manifest.Files[DiskFile].Chunks) {
		t.Fatalf("three waiters share one pull and zero chunks stay holes: %d gets, %d zero chunks", store.chunkGets(),
			zeroChunks)
	}
	cached, err := cache.Cached()
	if err != nil || len(cached) != 1 || cached[0] != manifest.Digest || !cache.Has(manifest.Digest) {
		t.Fatalf("cached: %v %v", cached, err)
	}
	if local.Manifest.Digest != manifest.Digest {
		t.Fatalf("local manifest: %+v", local.Manifest)
	}
}

type ensureWaiters struct {
	cache   *Cache
	digest  string
	results chan error
}

func (waiters *ensureWaiters) ensure() {
	_, err := waiters.cache.Ensure(context.Background(), waiters.digest)
	waiters.results <- err
}

func TestCorruptChunksFailThePullAndLeaveNothingHalfDone(t *testing.T) {
	ctx := context.Background()
	store := DirStore{Root: t.TempDir()}
	contents := snapshotContents("pages")
	manifest := publish(t, store, contents)
	diskChunk := manifest.Files[DiskFile].Chunks[0]
	key, _ := chunkKey(diskChunk.Digest)
	original, _ := readAll(ctx, store, key)
	if err := store.Put(ctx, key, strings.NewReader("tampered")); err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	cache := NewCache(store, root, linuxPool, 2)
	if _, err := cache.Ensure(ctx, manifest.Digest); err == nil || !strings.Contains(err.Error(), "corrupt") {
		t.Fatalf("corrupt chunk: %v", err)
	}
	if entries, _ := os.ReadDir(root); len(entries) != 0 {
		t.Fatalf("a failed pull leaves nothing behind: %v", entries)
	}
	if err := store.Put(ctx, key, bytes.NewReader(original)); err != nil {
		t.Fatal(err)
	}
	if _, err := cache.Ensure(ctx, manifest.Digest); err != nil {
		t.Fatalf("the next lease pulls again once the store is repaired: %v", err)
	}
	if err := store.Delete(ctx, key); err != nil {
		t.Fatal(err)
	}
	if _, err := NewCache(store, t.TempDir(), linuxPool, 2).Ensure(ctx, manifest.Digest); !errors.Is(err,
		ErrNotFound) {
		t.Fatalf("a missing chunk: %v", err)
	}
	if _, err := cache.Ensure(ctx, manifest.Digest); err != nil {
		t.Fatalf("a complete local copy no longer needs the store: %v", err)
	}
}

func TestSnapshotsOnlyRestoreWhereTheyWereBuilt(t *testing.T) {
	ctx := context.Background()
	store := DirStore{Root: t.TempDir()}
	manifest := publish(t, store, snapshotContents("pages"))
	upgraded := linuxPool
	upgraded.VMMVersion = "1.13.0"
	if _, err := NewCache(store, t.TempDir(), upgraded, 2).Ensure(ctx, manifest.Digest); !errors.Is(err,
		ErrIncompatible) || !strings.Contains(err.Error(), "1.12.0") {
		t.Fatalf("a VMM upgrade is a rebuild wave: %v", err)
	}
	root := t.TempDir()
	if _, err := NewCache(store, root, Compatibility{}, 2).Ensure(ctx, manifest.Digest); err != nil {
		t.Fatalf("a host without a declared binding takes any snapshot: %v", err)
	}
	if _, err := NewCache(store, root, upgraded, 2).Ensure(ctx, manifest.Digest); !errors.Is(err, ErrIncompatible) {
		t.Fatalf("a cached copy is checked too: %v", err)
	}
	if _, err := NewCache(store, root, linuxPool, 2).Ensure(ctx, "sha256:nope"); !errors.Is(err, ErrBadDigest) {
		t.Fatalf("bad digest: %v", err)
	}
}

func TestAWaiterThatGivesUpLeavesThePullRunning(t *testing.T) {
	inner := DirStore{Root: t.TempDir()}
	manifest := publish(t, inner, snapshotContents("pages"))
	store := newCountingStore(inner)
	store.release = make(chan struct{})
	cache := NewCache(store, t.TempDir(), linuxPool, 2)
	impatient, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if _, err := cache.Ensure(impatient, manifest.Digest); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("impatient waiter: %v", err)
	}
	close(store.release)
	if _, err := cache.Ensure(context.Background(), manifest.Digest); err != nil {
		t.Fatal(err)
	}
	if !cache.Has(manifest.Digest) {
		t.Fatal("the pull finished for the next waiter")
	}
}

func TestPinsHoldSnapshotsThatRunsReportsAndResultsReference(t *testing.T) {
	ctx := context.Background()
	store := DirStore{Root: t.TempDir()}
	manifest := publish(t, store, snapshotContents("pages"))
	pin, err := AddPin(ctx, store, manifest.Digest, "run:experiment_000001_run_000", builtAt)
	if err != nil || pin.Holder != "run:experiment_000001_run_000" {
		t.Fatalf("pin: %+v %v", pin, err)
	}
	again, err := AddPin(ctx, store, manifest.Digest, "run:experiment_000001_run_000", builtAt.Add(time.Hour))
	if err != nil || !again.PinnedAt.Equal(builtAt) {
		t.Fatalf("pinning again keeps the first pin: %+v %v", again, err)
	}
	if _, err := AddPin(ctx, store, manifest.Digest, "eval_report:experiment_000001_run_001", builtAt); err != nil {
		t.Fatal(err)
	}
	pins, err := Pins(ctx, store, manifest.Digest)
	if err != nil || len(pins) != 2 {
		t.Fatalf("pins: %+v %v", pins, err)
	}
	if err := RemovePin(ctx, store, manifest.Digest, "run:experiment_000001_run_000"); err != nil {
		t.Fatal(err)
	}
	if pins, _ := Pins(ctx, store, manifest.Digest); len(pins) != 1 {
		t.Fatalf("after unpinning: %+v", pins)
	}
	missing := "sha256:" + strings.Repeat("cd", 32)
	if _, err := AddPin(ctx, store, missing, "run:x", builtAt); !errors.Is(err, ErrNotFound) {
		t.Fatalf("pinning an unknown snapshot: %v", err)
	}
	if _, err := AddPin(ctx, store, manifest.Digest, "  ", builtAt); err == nil {
		t.Fatal("a pin needs a holder")
	}
}

func TestCollectionKeepsWhatIsCurrentPinnedOrYoungAndSweepsAfterGrace(t *testing.T) {
	ctx := context.Background()
	store := DirStore{Root: t.TempDir()}
	shared := publish(t, store, snapshotContents("shared"))
	pinned := publish(t, store, snapshotContents("pinned pages"))
	stale := publish(t, store, snapshotContents("stale pages"))
	if _, err := SetCurrent(ctx, store, shared, builtAt); err != nil {
		t.Fatal(err)
	}
	if _, err := AddPin(ctx, store, pinned.Digest, "published:recon_v1", builtAt); err != nil {
		t.Fatal(err)
	}
	policy := RetentionPolicy{MinAge: 7 * 24 * time.Hour, Grace: time.Hour}
	young, err := Collect(ctx, store, policy, builtAt.Add(24*time.Hour))
	if err != nil || len(young.Removed) != 0 {
		t.Fatalf("young snapshots stay: %+v %v", young, err)
	}
	chunksBefore := chunkCount(t, store)
	removal := eightDaysLater()
	report, err := Collect(ctx, store, policy, removal)
	if err != nil || len(report.Removed) != 1 || report.Removed[0] != stale.Digest || report.RemovedChunks != 0 {
		t.Fatalf("only the stale snapshot goes, its chunks wait out the grace: %+v %v", report, err)
	}
	if _, err := LoadManifest(ctx, store, stale.Digest); !errors.Is(err, ErrNotFound) {
		t.Fatalf("the removed manifest is no longer published: %v", err)
	}
	if _, err := AddPin(ctx, store, stale.Digest, "run:late", removal); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a removed snapshot cannot be pinned: %v", err)
	}
	reused := publishAt(t, store, snapshotContents("stale pages plus"), eightDaysLater)
	swept, err := Collect(ctx, store, policy, removal.Add(2*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	staleOnly := map[string]int64{}
	live := map[string]bool{}
	for _, manifest := range []Manifest{shared, pinned, reused} {
		for _, file := range manifest.Files {
			for _, chunk := range file.Chunks {
				live[chunk.Digest] = true
			}
		}
	}
	for _, file := range stale.Files {
		for _, chunk := range file.Chunks {
			if !live[chunk.Digest] {
				staleOnly[chunk.Digest] = chunk.Size
			}
		}
	}
	var freed int64
	for _, size := range staleOnly {
		freed += size
	}
	if swept.RemovedChunks != len(staleOnly) || swept.FreedBytes != freed || len(staleOnly) == 0 {
		t.Fatalf("the sweep frees exactly the chunks no live snapshot lists: %+v, expected %d", swept, len(staleOnly))
	}
	if chunkCount(t, store) != chunksBefore+chunkCountDelta(reused, stale, shared, pinned)-len(staleOnly) {
		t.Fatalf("chunk count after the sweep: %d", chunkCount(t, store))
	}
	for _, manifest := range []Manifest{shared, pinned, reused} {
		if _, err := NewCache(store, t.TempDir(), linuxPool, 2).Ensure(ctx, manifest.Digest); err != nil {
			t.Fatalf("%s must still pull after collection: %v", manifest.Digest, err)
		}
	}
	if trash, _ := store.List(ctx, "trash/"); len(trash) != 0 {
		t.Fatalf("expired trash is gone: %v", trash)
	}
}

// chunkCountDelta is how many chunks the reused snapshot added that none of the others has.
func chunkCountDelta(added Manifest, others ...Manifest) int {
	known := map[string]bool{}
	for _, manifest := range others {
		for _, file := range manifest.Files {
			for _, chunk := range file.Chunks {
				known[chunk.Digest] = true
			}
		}
	}
	fresh := map[string]bool{}
	for _, file := range added.Files {
		for _, chunk := range file.Chunks {
			if !known[chunk.Digest] {
				fresh[chunk.Digest] = true
			}
		}
	}
	return len(fresh)
}

// gatedStore holds back the chunks of one file until released, to observe a pull halfway through.
type gatedStore struct {
	ObjectStore
	held    map[string]bool
	release chan struct{}
	mu      sync.Mutex
	gets    []string
}

func (store *gatedStore) Get(ctx context.Context, key string) (io.ReadCloser, error) {
	store.mu.Lock()
	store.gets = append(store.gets, key)
	store.mu.Unlock()
	if store.held[key] {
		select {
		case <-store.release:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return store.ObjectStore.Get(ctx, key)
}

func (store *gatedStore) fetched(key string) int {
	store.mu.Lock()
	defer store.mu.Unlock()
	count := 0
	for _, got := range store.gets {
		if got == key {
			count++
		}
	}
	return count
}

func TestALazyOpenServesMemoryBeforeThePullFinishes(t *testing.T) {
	ctx := context.Background()
	inner := DirStore{Root: t.TempDir()}
	contents := map[string][]byte{
		StateFile:  []byte("device state"),
		MemoryFile: []byte("page one page two page three!!"),
		DiskFile:   []byte("the disk"),
	}
	manifest := publish(t, inner, contents)
	memoryChunks := manifest.Files[MemoryFile].Chunks
	held := map[string]bool{}
	for _, chunk := range memoryChunks {
		key, _ := chunkKey(chunk.Digest)
		held[key] = true
	}
	store := &gatedStore{ObjectStore: inner, held: held, release: make(chan struct{})}
	cache := NewCache(store, t.TempDir(), linuxPool, 1)
	lazy, err := cache.OpenLazy(ctx, manifest.Digest)
	if err != nil {
		t.Fatal(err)
	}
	defer lazy.Memory.Close()
	for name, path := range map[string]string{StateFile: lazy.StatePath, DiskFile: lazy.DiskPath} {
		if got, _ := os.ReadFile(path); !bytes.Equal(got, contents[name]) {
			t.Fatalf("%s is complete before the restore starts: %q", name, got)
		}
	}
	if cache.Has(manifest.Digest) || lazy.Memory.Size() != int64(len(contents[MemoryFile])) {
		t.Fatal("the snapshot is not complete while its memory is still held back")
	}
	close(store.release)
	page := make([]byte, 6)
	read, err := lazy.Memory.ReadAt(page, 9)
	if err != nil || read != 6 || string(page) != "page t" {
		t.Fatalf("a read past the first chunk: %q %d %v", page, read, err)
	}
	if _, err := cache.Ensure(ctx, manifest.Digest); err != nil {
		t.Fatal(err)
	}
	for _, chunk := range memoryChunks {
		key, _ := chunkKey(chunk.Digest)
		if store.fetched(key) != 1 {
			t.Fatalf("a chunk read lazily is not fetched again by the pull: %s fetched %d times", key,
				store.fetched(key))
		}
	}
	tail := make([]byte, 4)
	if read, err := lazy.Memory.ReadAt(tail, int64(len(contents[MemoryFile])-2)); read != 2 || !errors.Is(err, io.EOF) ||
		string(tail[:2]) != "!!" {
		t.Fatalf("reading over the end: %q %d %v", tail, read, err)
	}
	complete, err := cache.OpenLazy(ctx, manifest.Digest)
	if err != nil || complete.Memory != nil || complete.MemoryPath != lazy.MemoryPath {
		t.Fatalf("a complete snapshot opens with its memory file: %+v %v", complete, err)
	}
}

func TestALazyOpenFailsWithThePull(t *testing.T) {
	ctx := context.Background()
	store := DirStore{Root: t.TempDir()}
	manifest := publish(t, store, snapshotContents("pages"))
	key, _ := chunkKey(manifest.Files[StateFile].Chunks[0].Digest)
	if err := store.Delete(ctx, key); err != nil {
		t.Fatal(err)
	}
	if _, err := NewCache(store, t.TempDir(), linuxPool, 2).OpenLazy(ctx, manifest.Digest); !errors.Is(err,
		ErrNotFound) {
		t.Fatalf("a missing state chunk: %v", err)
	}
	if _, err := NewCache(store, t.TempDir(), linuxPool, 2).OpenLazy(ctx,
		"sha256:"+strings.Repeat("ef", 32)); !errors.Is(err, ErrNotFound) {
		t.Fatalf("an unknown snapshot: %v", err)
	}
	upgraded := linuxPool
	upgraded.CPUModel = "other"
	if _, err := NewCache(store, t.TempDir(), upgraded, 2).OpenLazy(ctx, manifest.Digest); !errors.Is(err,
		ErrIncompatible) {
		t.Fatalf("an incompatible snapshot: %v", err)
	}
}
