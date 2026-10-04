package snapshot

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"
	"time"
)

// DefaultChunkSize balances object count against how much two snapshots of one image share: 4 MiB.
const DefaultChunkSize int64 = 4 << 20

// DefaultWorkers is how many chunks move at once.
const DefaultWorkers = 8

// Publisher uploads snapshot files as chunks and records their manifest.
type Publisher struct {
	Store     ObjectStore
	ChunkSize int64
	Workers   int
	Now       func() time.Time
}

type chunkUpload struct {
	digest string
	data   []byte
}

// uploader stores the chunks the store lacks; several run at once.
type uploader struct {
	ctx   context.Context
	store ObjectStore
	jobs  chan chunkUpload
	group sync.WaitGroup
	mu    sync.Mutex
	errs  []error
}

func (upload *uploader) run() {
	defer upload.group.Done()
	for job := range upload.jobs {
		if err := upload.storeChunk(job); err != nil {
			upload.mu.Lock()
			upload.errs = append(upload.errs, err)
			upload.mu.Unlock()
		}
	}
}

func (upload *uploader) storeChunk(job chunkUpload) error {
	key, err := chunkKey(job.digest)
	if err != nil {
		return err
	}
	stored, err := upload.store.Exists(upload.ctx, key)
	if err != nil || stored {
		return err
	}
	return upload.store.Put(upload.ctx, key, bytes.NewReader(job.data))
}

func (publisher Publisher) chunkSize() int64 {
	if publisher.ChunkSize > 0 {
		return publisher.ChunkSize
	}
	return DefaultChunkSize
}

func (publisher Publisher) now() time.Time {
	if publisher.Now != nil {
		return publisher.Now().UTC()
	}
	return time.Now().UTC()
}

// Publish chunks the state, memory and disk files, uploads the chunks the store lacks and writes the manifest.
// Chunks are content addressed, so a snapshot shares every unchanged chunk with snapshots already stored.
func (publisher Publisher) Publish(ctx context.Context, draft Manifest, paths map[string]string) (Manifest, error) {
	if len(draft.ImageSpec) > 0 && !json.Valid(draft.ImageSpec) {
		return Manifest{}, fmt.Errorf("the image spec of %s is not JSON", draft.Image)
	}
	if _, err := digestHex(draft.ImageDigest); err != nil {
		return Manifest{}, fmt.Errorf("image digest: %w", err)
	}
	workers := publisher.Workers
	if workers <= 0 {
		workers = DefaultWorkers
	}
	upload := &uploader{ctx: ctx, store: publisher.Store, jobs: make(chan chunkUpload, workers)}
	for range workers {
		upload.group.Add(1)
		go upload.run()
	}
	manifest := draft
	manifest.Files = map[string]File{}
	seen := map[string]bool{}
	var chunkErr error
	for _, name := range SnapshotFiles {
		filePath, found := paths[name]
		if !found {
			chunkErr = fmt.Errorf("no %s file to publish", name)
			break
		}
		file, err := publisher.chunkFile(ctx, filePath, upload.jobs, seen)
		if err != nil {
			chunkErr = fmt.Errorf("chunk %s: %w", name, err)
			break
		}
		manifest.Files[name] = file
	}
	close(upload.jobs)
	upload.group.Wait()
	if err := errors.Join(append([]error{chunkErr}, upload.errs...)...); err != nil {
		return Manifest{}, err
	}
	manifest.BuiltAt = publisher.now()
	digest, err := manifest.ContentDigest()
	if err != nil {
		return Manifest{}, err
	}
	manifest.Digest = digest
	encoded, err := json.Marshal(manifest)
	if err != nil {
		return Manifest{}, err
	}
	key, err := manifestKey(digest)
	if err != nil {
		return Manifest{}, err
	}
	if err := publisher.Store.Put(ctx, key, bytes.NewReader(encoded)); err != nil {
		return Manifest{}, err
	}
	return manifest, nil
}

// chunkFile hashes one file chunk by chunk and queues each chunk not queued before for upload.
func (publisher Publisher) chunkFile(ctx context.Context, filePath string, jobs chan<- chunkUpload,
	seen map[string]bool) (File, error) {
	opened, err := os.Open(filePath)
	if err != nil {
		return File{}, err
	}
	defer opened.Close()
	var file File
	for {
		if err := ctx.Err(); err != nil {
			return File{}, err
		}
		buffer := make([]byte, publisher.chunkSize())
		read, err := io.ReadFull(opened, buffer)
		if read > 0 {
			sum := sha256.Sum256(buffer[:read])
			digest := digestPrefix + hex.EncodeToString(sum[:])
			file.Chunks = append(file.Chunks, Chunk{Digest: digest, Size: int64(read)})
			file.Size += int64(read)
			if !seen[digest] {
				seen[digest] = true
				jobs <- chunkUpload{digest: digest, data: buffer[:read]}
			}
		}
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return file, nil
		}
		if err != nil {
			return File{}, err
		}
	}
}
