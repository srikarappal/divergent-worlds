// Package snapshot is the snapshot store (DESIGN.md sections 3 and 6.2).
//
// A snapshot is a VM's device state, guest memory and disk captured together at
// "app ready". Its files are split into fixed size chunks stored once under
// their sha256 in object storage; a manifest lists each file's chunks plus what
// the snapshot is bound to (pool, VMM version, machine type and CPU model) and
// the snapshot's digest is the sha256 of that content. Hosts pull a snapshot to
// local NVMe the first time a lease needs it or when a run is scheduled,
// verifying every chunk. Pins keep the snapshots that runs, eval reports and
// published results reference; collection removes the rest once they age out.
package snapshot

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
)

// ErrNotFound means the object store holds nothing under a key.
var ErrNotFound = errors.New("object not found")

// ObjectStore is the slice of object storage the snapshot store uses.
type ObjectStore interface {
	Put(ctx context.Context, key string, body io.Reader) error
	Get(ctx context.Context, key string) (io.ReadCloser, error)
	Exists(ctx context.Context, key string) (bool, error)
	Delete(ctx context.Context, key string) error
	List(ctx context.Context, prefix string) ([]string, error)
}

// DirStore keeps objects as files under a directory: a mounted bucket in production, a temp dir in tests.
type DirStore struct {
	Root string
}

const temporaryPrefix = ".partial-"

func (store DirStore) path(key string) (string, error) {
	if key == "" || path.IsAbs(key) || path.Clean(key) != key || strings.HasPrefix(key, "../") || key == ".." {
		return "", fmt.Errorf("object key %q is not a clean relative key", key)
	}
	if strings.HasPrefix(path.Base(key), temporaryPrefix) {
		return "", fmt.Errorf("object key %q uses the reserved %s prefix", key, temporaryPrefix)
	}
	return filepath.Join(store.Root, filepath.FromSlash(key)), nil
}

// Put writes the whole body under a temporary name, then renames it, so readers never see half an object.
func (store DirStore) Put(ctx context.Context, key string, body io.Reader) error {
	target, err := store.path(key)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(target), temporaryPrefix+"*")
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(temporary, contextReader{ctx: ctx, reader: body})
	closeErr := temporary.Close()
	if err := errors.Join(copyErr, closeErr); err != nil {
		os.Remove(temporary.Name())
		return fmt.Errorf("put %s: %w", key, err)
	}
	if err := os.Chmod(temporary.Name(), 0o644); err != nil {
		os.Remove(temporary.Name())
		return err
	}
	return os.Rename(temporary.Name(), target)
}

// Get opens an object for reading.
func (store DirStore) Get(_ context.Context, key string) (io.ReadCloser, error) {
	target, err := store.path(key)
	if err != nil {
		return nil, err
	}
	opened, err := os.Open(target)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, fmt.Errorf("%w: %s", ErrNotFound, key)
	}
	return opened, err
}

// Exists reports whether an object is stored under key.
func (store DirStore) Exists(_ context.Context, key string) (bool, error) {
	target, err := store.path(key)
	if err != nil {
		return false, err
	}
	_, err = os.Stat(target)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// Delete removes an object; deleting what is already gone is not an error.
func (store DirStore) Delete(_ context.Context, key string) error {
	target, err := store.path(key)
	if err != nil {
		return err
	}
	if err := os.Remove(target); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return nil
}

// List returns every key starting with prefix, sorted.
func (store DirStore) List(_ context.Context, prefix string) ([]string, error) {
	walker := keyWalker{root: store.Root, prefix: prefix}
	start := filepath.Join(store.Root, filepath.FromSlash(path.Dir(prefix+"x")))
	if err := filepath.WalkDir(start, walker.visit); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	sort.Strings(walker.keys)
	return walker.keys, nil
}

type keyWalker struct {
	root   string
	prefix string
	keys   []string
}

func (walker *keyWalker) visit(walked string, entry fs.DirEntry, err error) error {
	if err != nil {
		return err
	}
	if entry.IsDir() || strings.HasPrefix(entry.Name(), temporaryPrefix) {
		return nil
	}
	relative, err := filepath.Rel(walker.root, walked)
	if err != nil {
		return err
	}
	if key := filepath.ToSlash(relative); strings.HasPrefix(key, walker.prefix) {
		walker.keys = append(walker.keys, key)
	}
	return nil
}

// contextReader stops a long copy once its context ends.
type contextReader struct {
	ctx    context.Context
	reader io.Reader
}

func (reader contextReader) Read(buffer []byte) (int, error) {
	if err := reader.ctx.Err(); err != nil {
		return 0, err
	}
	return reader.reader.Read(buffer)
}

// readAll reads one whole object.
func readAll(ctx context.Context, store ObjectStore, key string) ([]byte, error) {
	body, err := store.Get(ctx, key)
	if err != nil {
		return nil, err
	}
	defer body.Close()
	return io.ReadAll(body)
}
