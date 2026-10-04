package snapshot

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

// The files every snapshot has: device state, guest memory and the disk captured with that memory.
const (
	StateFile  = "state"
	MemoryFile = "memory"
	DiskFile   = "disk"
)

// SnapshotFiles lists the file roles in a fixed order.
var SnapshotFiles = []string{StateFile, MemoryFile, DiskFile}

const digestPrefix = "sha256:"

var (
	// ErrBadDigest means a digest is not sha256:<64 hex>.
	ErrBadDigest = errors.New("not a sha256 digest")
	// ErrIncompatible means a snapshot was built for another pool, VMM version, machine type or CPU model.
	ErrIncompatible = errors.New("snapshot is incompatible with this host")
)

// Chunk is one piece of a file, stored once under its sha256.
type Chunk struct {
	Digest string `json:"digest"`
	Size   int64  `json:"size"`
}

// File is one snapshot file as its chunks in order.
type File struct {
	Size   int64   `json:"size"`
	Chunks []Chunk `json:"chunks"`
}

// Compatibility is what a snapshot is bound to (DESIGN.md 6.1.6); restoring it anywhere else fails or corrupts.
type Compatibility struct {
	Pool        string `json:"pool"`
	VMM         string `json:"vmm"`
	VMMVersion  string `json:"vmm_version"`
	MachineType string `json:"machine_type,omitempty"`
	CPUModel    string `json:"cpu_model"`
}

// Describe names the binding for error messages.
func (compatibility Compatibility) Describe() string {
	parts := []string{compatibility.Pool, compatibility.VMM + " " + compatibility.VMMVersion, compatibility.CPUModel}
	if compatibility.MachineType != "" {
		parts = append(parts, compatibility.MachineType)
	}
	return strings.Join(parts, ", ")
}

// Manifest describes one snapshot; Digest is the sha256 of everything in it but itself and BuiltAt.
type Manifest struct {
	Digest        string          `json:"digest"`
	Image         string          `json:"image"`
	ImageDigest   string          `json:"image_digest"`
	ImageSpec     json.RawMessage `json:"image_spec,omitempty"`
	Compatibility Compatibility   `json:"compatibility"`
	MemoryMiB     int             `json:"memory_mib"`
	VCPUs         int             `json:"vcpus"`
	Files         map[string]File `json:"files"`
	BuiltAt       time.Time       `json:"built_at"`
}

// ContentDigest is the snapshot's identity: the same files and bindings always give the same digest.
func (manifest Manifest) ContentDigest() (string, error) {
	content := manifest
	content.Digest = ""
	content.BuiltAt = time.Time{}
	encoded, err := json.Marshal(content)
	if err != nil {
		return "", fmt.Errorf("encode the manifest of %s: %w", manifest.Image, err)
	}
	sum := sha256.Sum256(encoded)
	return digestPrefix + hex.EncodeToString(sum[:]), nil
}

// Bytes is the total size of the snapshot's files.
func (manifest Manifest) Bytes() int64 {
	var total int64
	for _, file := range manifest.Files {
		total += file.Size
	}
	return total
}

// CheckCompatible refuses a snapshot built for another binding.
func (manifest Manifest) CheckCompatible(host Compatibility) error {
	if manifest.Compatibility != host {
		return fmt.Errorf("%w: %s was built for %s, this host is %s", ErrIncompatible, manifest.Digest,
			manifest.Compatibility.Describe(), host.Describe())
	}
	return nil
}

// validate checks a manifest read from the store before anything trusts its chunk list.
func (manifest Manifest) validate() error {
	if digest, err := manifest.ContentDigest(); err != nil || digest != manifest.Digest {
		return fmt.Errorf("manifest %s does not match its content", manifest.Digest)
	}
	for _, name := range SnapshotFiles {
		file, found := manifest.Files[name]
		if !found {
			return fmt.Errorf("manifest %s has no %s file", manifest.Digest, name)
		}
		var total int64
		for _, chunk := range file.Chunks {
			if _, err := digestHex(chunk.Digest); err != nil || chunk.Size <= 0 {
				return fmt.Errorf("manifest %s lists a bad chunk in %s", manifest.Digest, name)
			}
			total += chunk.Size
		}
		if total != file.Size {
			return fmt.Errorf("manifest %s: the %s chunks add up to %d bytes, not %d", manifest.Digest, name, total,
				file.Size)
		}
	}
	return nil
}

// CheckDigest refuses anything but sha256:<64 hex>.
func CheckDigest(digest string) error {
	_, err := digestHex(digest)
	return err
}

// digestHex is the hex part of a sha256 digest.
func digestHex(digest string) (string, error) {
	hexPart, found := strings.CutPrefix(digest, digestPrefix)
	if !found || len(hexPart) != sha256.Size*2 {
		return "", fmt.Errorf("%w: %q", ErrBadDigest, digest)
	}
	if _, err := hex.DecodeString(hexPart); err != nil {
		return "", fmt.Errorf("%w: %q", ErrBadDigest, digest)
	}
	return hexPart, nil
}

func chunkKey(digest string) (string, error) {
	hexPart, err := digestHex(digest)
	if err != nil {
		return "", err
	}
	return "chunks/" + hexPart, nil
}

func manifestKey(digest string) (string, error) {
	hexPart, err := digestHex(digest)
	if err != nil {
		return "", err
	}
	return "snapshots/" + hexPart + ".json", nil
}

// LoadManifest reads and checks one snapshot's manifest.
func LoadManifest(ctx context.Context, store ObjectStore, digest string) (Manifest, error) {
	key, err := manifestKey(digest)
	if err != nil {
		return Manifest{}, err
	}
	raw, err := readAll(ctx, store, key)
	if err != nil {
		return Manifest{}, err
	}
	var manifest Manifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return Manifest{}, fmt.Errorf("manifest %s: %w", digest, err)
	}
	if manifest.Digest != digest {
		return Manifest{}, fmt.Errorf("manifest stored under %s names %s", digest, manifest.Digest)
	}
	return manifest, manifest.validate()
}

// ListManifests reads every snapshot manifest in the store.
func ListManifests(ctx context.Context, store ObjectStore) ([]Manifest, error) {
	keys, err := store.List(ctx, "snapshots/")
	if err != nil {
		return nil, err
	}
	var manifests []Manifest
	for _, key := range keys {
		hexPart := strings.TrimSuffix(strings.TrimPrefix(key, "snapshots/"), ".json")
		manifest, err := LoadManifest(ctx, store, digestPrefix+hexPart)
		if err != nil {
			return nil, err
		}
		manifests = append(manifests, manifest)
	}
	return manifests, nil
}
