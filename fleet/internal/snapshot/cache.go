package snapshot

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"sync"
)

const manifestFileName = "manifest.json"

// pullOrder fetches what a restore cannot start without first: the state and the disk the clone's overlay copies.
// Memory comes last, since a lazy restore serves it page by page meanwhile.
var pullOrder = []string{StateFile, DiskFile, MemoryFile}

// Local is a pulled snapshot's files on this host.
type Local struct {
	Manifest   Manifest
	StatePath  string
	MemoryPath string
	DiskPath   string
}

// LazySnapshot is a snapshot whose state and disk are local while its memory may still be arriving.
type LazySnapshot struct {
	Local
	// Memory reads guest memory while the pull goes on; nil when the snapshot was already complete on this host.
	Memory *LazyMemory
}

// Cache pulls snapshots from the store to local NVMe: lazily, the first time a lease or a prefetch needs one,
// and once however many ask at the same time. Every chunk is checked against its digest before it lands.
type Cache struct {
	store       ObjectStore
	root        string
	host        Compatibility
	parallelism int
	mu          sync.Mutex
	pulls       map[string]*pull
	zeroDigests map[int64]string
}

// pull is one snapshot on its way to this host; lazy restores read through its assembly before it is done.
type pull struct {
	started   chan struct{}
	startOnce sync.Once
	done      chan struct{}
	assembly  *assembly
	local     Local
	err       error
}

func (current *pull) closeStarted() {
	close(current.started)
}

func (current *pull) markStarted() {
	current.startOnce.Do(current.closeStarted)
}

// NewCache returns a cache under root; a host Compatibility with an empty pool accepts any snapshot.
func NewCache(store ObjectStore, root string, host Compatibility, parallelism int) *Cache {
	if parallelism <= 0 {
		parallelism = DefaultWorkers
	}
	return &Cache{store: store, root: root, host: host, parallelism: parallelism, pulls: map[string]*pull{},
		zeroDigests: map[int64]string{}}
}

// Host is the binding this cache accepts.
func (cache *Cache) Host() Compatibility {
	return cache.host
}

// Store is where the cache pulls from.
func (cache *Cache) Store() ObjectStore {
	return cache.store
}

func (cache *Cache) dir(digest string) (string, error) {
	hexPart, err := digestHex(digest)
	if err != nil {
		return "", err
	}
	return filepath.Join(cache.root, hexPart), nil
}

func localFiles(dir string, manifest Manifest) Local {
	return Local{Manifest: manifest, StatePath: filepath.Join(dir, StateFile),
		MemoryPath: filepath.Join(dir, MemoryFile), DiskPath: filepath.Join(dir, DiskFile)}
}

// Lookup returns a snapshot already complete on this host; its manifest is written last, so it marks completion.
func (cache *Cache) Lookup(digest string) (Local, error) {
	dir, err := cache.dir(digest)
	if err != nil {
		return Local{}, err
	}
	raw, err := os.ReadFile(filepath.Join(dir, manifestFileName))
	if err != nil {
		return Local{}, err
	}
	var manifest Manifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return Local{}, fmt.Errorf("cached manifest of %s: %w", digest, err)
	}
	return localFiles(dir, manifest), nil
}

// Has reports whether a snapshot is complete on this host.
func (cache *Cache) Has(digest string) bool {
	_, err := cache.Lookup(digest)
	return err == nil
}

// Cached lists the snapshots complete on this host, which the scheduler ranks hosts by.
func (cache *Cache) Cached() ([]string, error) {
	entries, err := os.ReadDir(cache.root)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var digests []string
	for _, entry := range entries {
		digest := digestPrefix + entry.Name()
		if entry.IsDir() && cache.Has(digest) {
			digests = append(digests, digest)
		}
	}
	sort.Strings(digests)
	return digests, nil
}

// Ensure returns the snapshot's local files, pulling it first when this host does not have it yet. A caller that
// gives up stops waiting; the pull goes on for the others waiting and for the next lease.
func (cache *Cache) Ensure(ctx context.Context, digest string) (Local, error) {
	if local, err := cache.Lookup(digest); err == nil {
		return local, cache.checkCompatible(local.Manifest)
	}
	current, err := cache.startPull(digest)
	if err != nil {
		return Local{}, err
	}
	select {
	case <-current.done:
		return current.local, current.err
	case <-ctx.Done():
		return Local{}, ctx.Err()
	}
}

// OpenLazy returns once the state and disk are local; guest memory is read through Memory, which fetches the
// chunks a read needs ahead of the rest of the pull (DESIGN.md 6.1.2, lazy loading while the file is remote).
func (cache *Cache) OpenLazy(ctx context.Context, digest string) (LazySnapshot, error) {
	if local, err := cache.Lookup(digest); err == nil {
		return LazySnapshot{Local: local}, cache.checkCompatible(local.Manifest)
	}
	current, err := cache.startPull(digest)
	if err != nil {
		return LazySnapshot{}, err
	}
	select {
	case <-current.started:
	case <-ctx.Done():
		return LazySnapshot{}, ctx.Err()
	}
	if current.assembly == nil {
		<-current.done
		return LazySnapshot{}, current.err
	}
	for _, name := range []string{StateFile, DiskFile} {
		select {
		case <-current.assembly.complete[name]:
		case <-current.done:
			if current.err != nil {
				return LazySnapshot{}, current.err
			}
		case <-ctx.Done():
			return LazySnapshot{}, ctx.Err()
		}
	}
	memory, err := current.assembly.openMemory()
	if err != nil {
		return LazySnapshot{}, err
	}
	return LazySnapshot{Local: localFiles(current.assembly.dir, current.assembly.manifest), Memory: memory}, nil
}

func (cache *Cache) startPull(digest string) (*pull, error) {
	if _, err := digestHex(digest); err != nil {
		return nil, err
	}
	cache.mu.Lock()
	defer cache.mu.Unlock()
	current, pulling := cache.pulls[digest]
	if !pulling {
		current = &pull{started: make(chan struct{}), done: make(chan struct{})}
		cache.pulls[digest] = current
		go cache.run(digest, current)
	}
	return current, nil
}

func (cache *Cache) run(digest string, current *pull) {
	current.local, current.err = cache.pull(context.Background(), digest, current)
	current.markStarted()
	cache.mu.Lock()
	delete(cache.pulls, digest)
	cache.mu.Unlock()
	close(current.done)
}

func (cache *Cache) checkCompatible(manifest Manifest) error {
	if cache.host.Pool == "" {
		return nil
	}
	return manifest.CheckCompatible(cache.host)
}

// pull assembles the files in place; the manifest written last marks the snapshot complete.
func (cache *Cache) pull(ctx context.Context, digest string, current *pull) (Local, error) {
	manifest, err := LoadManifest(ctx, cache.store, digest)
	if err != nil {
		return Local{}, err
	}
	if err := cache.checkCompatible(manifest); err != nil {
		return Local{}, err
	}
	dir, err := cache.dir(digest)
	if err != nil {
		return Local{}, err
	}
	if err := os.RemoveAll(dir); err != nil {
		return Local{}, err
	}
	assembled, err := newAssembly(cache, manifest, dir)
	if err != nil {
		os.RemoveAll(dir)
		return Local{}, err
	}
	current.assembly = assembled
	current.markStarted()
	if err := assembled.fillAll(ctx); err != nil {
		assembled.close()
		os.RemoveAll(dir)
		return Local{}, err
	}
	if err := assembled.finish(); err != nil {
		os.RemoveAll(dir)
		return Local{}, err
	}
	return localFiles(dir, manifest), nil
}

// zeroDigest is the digest of a chunk of zeros; such chunks stay holes in the sparse local file.
func (cache *Cache) zeroDigest(size int64) string {
	cache.mu.Lock()
	defer cache.mu.Unlock()
	if digest, found := cache.zeroDigests[size]; found {
		return digest
	}
	sum := sha256.Sum256(make([]byte, size))
	digest := digestPrefix + hex.EncodeToString(sum[:])
	cache.zeroDigests[size] = digest
	return digest
}

func writeManifest(dir string, manifest Manifest) error {
	encoded, err := json.Marshal(manifest)
	if err != nil {
		return err
	}
	temporary := filepath.Join(dir, "."+manifestFileName)
	if err := os.WriteFile(temporary, encoded, 0o644); err != nil {
		return err
	}
	return os.Rename(temporary, filepath.Join(dir, manifestFileName))
}

// chunkSlot is one chunk of one file being assembled.
type chunkSlot struct {
	file   string
	index  int
	offset int64
	chunk  Chunk
}

// assembly is one snapshot being pulled: its files exist at full size from the start and fill in chunk by chunk,
// so a reader can ask for any range before the whole pull is done.
type assembly struct {
	cache     *Cache
	manifest  Manifest
	dir       string
	files     map[string]*os.File
	slots     map[string][]chunkSlot
	complete  map[string]chan struct{}
	mu        sync.Mutex
	present   map[string][]bool
	remaining map[string]int
	fetching  map[chunkSlot]chan struct{}
}

func newAssembly(cache *Cache, manifest Manifest, dir string) (*assembly, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	assembled := &assembly{cache: cache, manifest: manifest, dir: dir, files: map[string]*os.File{},
		slots: map[string][]chunkSlot{}, complete: map[string]chan struct{}{}, present: map[string][]bool{},
		remaining: map[string]int{}, fetching: map[chunkSlot]chan struct{}{}}
	for _, name := range SnapshotFiles {
		file := manifest.Files[name]
		created, err := os.OpenFile(filepath.Join(dir, name), os.O_RDWR|os.O_CREATE|os.O_TRUNC, 0o644)
		if err != nil {
			assembled.close()
			return nil, err
		}
		assembled.files[name] = created
		if err := created.Truncate(file.Size); err != nil {
			assembled.close()
			return nil, err
		}
		var offset int64
		for index, chunk := range file.Chunks {
			assembled.slots[name] = append(assembled.slots[name], chunkSlot{file: name, index: index, offset: offset,
				chunk: chunk})
			offset += chunk.Size
		}
		assembled.present[name] = make([]bool, len(file.Chunks))
		assembled.remaining[name] = len(file.Chunks)
		assembled.complete[name] = make(chan struct{})
		if len(file.Chunks) == 0 {
			close(assembled.complete[name])
		}
	}
	return assembled, nil
}

func (assembled *assembly) close() {
	for _, file := range assembled.files {
		file.Close()
	}
}

// ensureChunk fetches one chunk unless it is present; concurrent askers for the same chunk share one fetch.
func (assembled *assembly) ensureChunk(ctx context.Context, slot chunkSlot) error {
	for {
		assembled.mu.Lock()
		if assembled.present[slot.file][slot.index] {
			assembled.mu.Unlock()
			return nil
		}
		waiting, inFlight := assembled.fetching[slot]
		if !inFlight {
			waiting = make(chan struct{})
			assembled.fetching[slot] = waiting
		}
		assembled.mu.Unlock()
		if inFlight {
			select {
			case <-waiting:
				continue
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		err := assembled.fetch(ctx, slot)
		assembled.mu.Lock()
		if err == nil {
			assembled.present[slot.file][slot.index] = true
			assembled.remaining[slot.file]--
			if assembled.remaining[slot.file] == 0 {
				close(assembled.complete[slot.file])
			}
		}
		delete(assembled.fetching, slot)
		assembled.mu.Unlock()
		close(waiting)
		return err
	}
}

// ensureRange fetches every chunk of a file that overlaps [offset, offset+length).
func (assembled *assembly) ensureRange(ctx context.Context, name string, offset, length int64) error {
	slots := assembled.slots[name]
	low, high := 0, len(slots)
	for low < high {
		middle := (low + high) / 2
		if slots[middle].offset+slots[middle].chunk.Size <= offset {
			low = middle + 1
		} else {
			high = middle
		}
	}
	for index := low; index < len(slots) && slots[index].offset < offset+length; index++ {
		if err := assembled.ensureChunk(ctx, slots[index]); err != nil {
			return err
		}
	}
	return nil
}

// fetch reads one chunk from the store, checks its size and digest, then writes it at its offset.
func (assembled *assembly) fetch(ctx context.Context, slot chunkSlot) error {
	if slot.chunk.Digest == assembled.cache.zeroDigest(slot.chunk.Size) {
		return nil
	}
	key, err := chunkKey(slot.chunk.Digest)
	if err != nil {
		return err
	}
	body, err := assembled.cache.store.Get(ctx, key)
	if err != nil {
		return err
	}
	defer body.Close()
	data, err := io.ReadAll(io.LimitReader(body, slot.chunk.Size+1))
	if err != nil {
		return err
	}
	sum := sha256.Sum256(data)
	if int64(len(data)) != slot.chunk.Size || digestPrefix+hex.EncodeToString(sum[:]) != slot.chunk.Digest {
		return fmt.Errorf("chunk %s of %s is corrupt in the store", slot.chunk.Digest, slot.file)
	}
	_, err = assembled.files[slot.file].WriteAt(data, slot.offset)
	return err
}

// fillAll fetches every chunk not yet present with the cache's parallelism, state and disk first.
func (assembled *assembly) fillAll(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	slots := make(chan chunkSlot)
	filler := &chunkFiller{assembled: assembled, ctx: ctx, cancel: cancel, slots: slots}
	for range assembled.cache.parallelism {
		filler.group.Add(1)
		go filler.run()
	}
	for _, name := range pullOrder {
		for _, slot := range assembled.slots[name] {
			select {
			case slots <- slot:
			case <-ctx.Done():
			}
		}
	}
	close(slots)
	filler.group.Wait()
	return filler.err
}

// chunkFiller runs the fetches of one fillAll; the first failure cancels the rest.
type chunkFiller struct {
	assembled *assembly
	ctx       context.Context
	cancel    context.CancelFunc
	slots     chan chunkSlot
	group     sync.WaitGroup
	mu        sync.Mutex
	err       error
}

func (filler *chunkFiller) run() {
	defer filler.group.Done()
	for slot := range filler.slots {
		if filler.ctx.Err() != nil {
			continue
		}
		if err := filler.assembled.ensureChunk(filler.ctx, slot); err != nil {
			filler.mu.Lock()
			if filler.err == nil {
				filler.err = err
			}
			filler.mu.Unlock()
			filler.cancel()
		}
	}
}

// finish flushes the files, then writes the manifest that marks the snapshot complete.
func (assembled *assembly) finish() error {
	var errs []error
	for _, file := range assembled.files {
		errs = append(errs, file.Sync(), file.Close())
	}
	if err := errors.Join(errs...); err != nil {
		return err
	}
	return writeManifest(assembled.dir, assembled.manifest)
}

func (assembled *assembly) openMemory() (*LazyMemory, error) {
	file, err := os.Open(filepath.Join(assembled.dir, MemoryFile))
	if err != nil {
		return nil, err
	}
	return &LazyMemory{assembled: assembled, file: file}, nil
}

// LazyMemory reads a snapshot's memory file while it is still being pulled, fetching the chunks a read needs
// ahead of the rest of the pull. It keeps its own handle, so it outlives the pull.
type LazyMemory struct {
	assembled *assembly
	file      *os.File
}

// ReadAt reads guest memory at offset once the chunks under it have landed.
func (memory *LazyMemory) ReadAt(buffer []byte, offset int64) (int, error) {
	if err := memory.assembled.ensureRange(context.Background(), MemoryFile, offset, int64(len(buffer))); err != nil {
		return 0, err
	}
	return memory.file.ReadAt(buffer, offset)
}

// Size is the memory file's size.
func (memory *LazyMemory) Size() int64 {
	return memory.assembled.manifest.Files[MemoryFile].Size
}

// Close releases the handle; the clone's pages are already in guest memory.
func (memory *LazyMemory) Close() error {
	return memory.file.Close()
}
