package guest

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
)

// FilesystemFrames writes frames as blobs/<first two hex>/<hex>, the layout the Python object store reads.
type FilesystemFrames struct {
	root string
}

// NewFilesystemFrames stores frames under root.
func NewFilesystemFrames(root string) *FilesystemFrames {
	return &FilesystemFrames{root: root}
}

// Put stores a frame once; identical screens share one blob.
func (frames *FilesystemFrames) Put(data []byte) (string, error) {
	sum := sha256.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	target := filepath.Join(frames.root, "blobs", digest[:2], digest)
	if _, err := os.Stat(target); err == nil {
		return digest, nil
	} else if !errors.Is(err, fs.ErrNotExist) {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return "", err
	}
	partial, err := os.CreateTemp(filepath.Dir(target), digest+".*.partial")
	if err != nil {
		return "", err
	}
	if _, err := partial.Write(data); err != nil {
		partial.Close()
		os.Remove(partial.Name())
		return "", err
	}
	if err := partial.Close(); err != nil {
		os.Remove(partial.Name())
		return "", err
	}
	return digest, os.Rename(partial.Name(), target)
}
