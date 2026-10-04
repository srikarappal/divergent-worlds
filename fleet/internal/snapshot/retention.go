package snapshot

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

// Pin records that something references a snapshot: a training run, an eval report or a published result
// (DESIGN.md 6.2.7). Specs alone do not reproduce worlds, because installers and package mirrors change.
type Pin struct {
	Snapshot string    `json:"snapshot"`
	Holder   string    `json:"holder"`
	PinnedAt time.Time `json:"pinned_at"`
}

// RetentionPolicy says when collection may remove a snapshot nothing pins and no image uses as current.
type RetentionPolicy struct {
	// MinAge keeps unpinned snapshots younger than this.
	MinAge time.Duration
	// Grace is how long a removed snapshot's chunks wait before they go, so a publish that reused one of them
	// while the snapshot was being removed has written its own manifest by then.
	Grace time.Duration
}

// CollectReport says what one collection removed.
type CollectReport struct {
	Removed       []string `json:"removed"`
	RemovedChunks int      `json:"removed_chunks"`
	FreedBytes    int64    `json:"freed_bytes"`
}

// trashed is a removed snapshot's manifest waiting out the grace period.
type trashed struct {
	Manifest  Manifest  `json:"manifest"`
	TrashedAt time.Time `json:"trashed_at"`
}

func pinPrefix(snapshotDigest string) (string, error) {
	hexPart, err := digestHex(snapshotDigest)
	if err != nil {
		return "", err
	}
	return "pins/" + hexPart + "/", nil
}

func pinKey(snapshotDigest, holder string) (string, error) {
	prefix, err := pinPrefix(snapshotDigest)
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(holder) == "" {
		return "", errors.New("a pin needs a holder such as run:<run id>")
	}
	holderSum := sha256.Sum256([]byte(holder))
	return prefix + hex.EncodeToString(holderSum[:16]) + ".json", nil
}

// AddPin pins a published snapshot for a holder; pinning again keeps the first pin.
func AddPin(ctx context.Context, store ObjectStore, snapshotDigest, holder string, now time.Time) (Pin, error) {
	key, err := pinKey(snapshotDigest, holder)
	if err != nil {
		return Pin{}, err
	}
	manifest, err := manifestKey(snapshotDigest)
	if err != nil {
		return Pin{}, err
	}
	published, err := store.Exists(ctx, manifest)
	if err != nil {
		return Pin{}, err
	}
	if !published {
		return Pin{}, fmt.Errorf("%w: snapshot %s", ErrNotFound, snapshotDigest)
	}
	if raw, err := readAll(ctx, store, key); err == nil {
		var existing Pin
		return existing, json.Unmarshal(raw, &existing)
	}
	pin := Pin{Snapshot: snapshotDigest, Holder: holder, PinnedAt: now.UTC()}
	encoded, err := json.Marshal(pin)
	if err != nil {
		return Pin{}, err
	}
	return pin, store.Put(ctx, key, bytes.NewReader(encoded))
}

// RemovePin drops one holder's pin.
func RemovePin(ctx context.Context, store ObjectStore, snapshotDigest, holder string) error {
	key, err := pinKey(snapshotDigest, holder)
	if err != nil {
		return err
	}
	return store.Delete(ctx, key)
}

// Pins lists who pins a snapshot.
func Pins(ctx context.Context, store ObjectStore, snapshotDigest string) ([]Pin, error) {
	prefix, err := pinPrefix(snapshotDigest)
	if err != nil {
		return nil, err
	}
	keys, err := store.List(ctx, prefix)
	if err != nil {
		return nil, err
	}
	var pins []Pin
	for _, key := range keys {
		raw, err := readAll(ctx, store, key)
		if err != nil {
			return nil, err
		}
		var pin Pin
		if err := json.Unmarshal(raw, &pin); err != nil {
			return nil, fmt.Errorf("%s: %w", key, err)
		}
		pins = append(pins, pin)
	}
	return pins, nil
}

// Collect removes the snapshots no pin holds, no image uses as current and that are older than MinAge, then
// deletes the chunks of snapshots removed at least Grace ago that no remaining snapshot lists.
func Collect(ctx context.Context, store ObjectStore, policy RetentionPolicy, now time.Time) (CollectReport, error) {
	report := CollectReport{Removed: []string{}}
	manifests, err := ListManifests(ctx, store)
	if err != nil {
		return report, err
	}
	currents, err := ListCurrent(ctx, store)
	if err != nil {
		return report, err
	}
	current := map[string]bool{}
	for _, entry := range currents {
		current[entry.Snapshot] = true
	}
	for _, manifest := range manifests {
		if current[manifest.Digest] || now.Sub(manifest.BuiltAt) < policy.MinAge {
			continue
		}
		pins, err := Pins(ctx, store, manifest.Digest)
		if err != nil {
			return report, err
		}
		if len(pins) > 0 {
			continue
		}
		if err := trash(ctx, store, manifest, now); err != nil {
			return report, err
		}
		report.Removed = append(report.Removed, manifest.Digest)
	}
	return report, sweepTrash(ctx, store, policy, now, &report)
}

func trashKey(digest string) (string, error) {
	hexPart, err := digestHex(digest)
	if err != nil {
		return "", err
	}
	return "trash/" + hexPart + ".json", nil
}

// trash moves a manifest out of the published set; its chunks stay until the grace period ends.
func trash(ctx context.Context, store ObjectStore, manifest Manifest, now time.Time) error {
	key, err := trashKey(manifest.Digest)
	if err != nil {
		return err
	}
	encoded, err := json.Marshal(trashed{Manifest: manifest, TrashedAt: now.UTC()})
	if err != nil {
		return err
	}
	if err := store.Put(ctx, key, bytes.NewReader(encoded)); err != nil {
		return err
	}
	published, err := manifestKey(manifest.Digest)
	if err != nil {
		return err
	}
	return store.Delete(ctx, published)
}

// sweepTrash deletes the chunks only expired trash lists, counted once each, then the expired trash itself.
func sweepTrash(ctx context.Context, store ObjectStore, policy RetentionPolicy, now time.Time,
	report *CollectReport) error {
	keys, err := store.List(ctx, "trash/")
	if err != nil {
		return err
	}
	live, err := ListManifests(ctx, store)
	if err != nil {
		return err
	}
	referenced := map[string]bool{}
	for _, manifest := range live {
		for _, file := range manifest.Files {
			for _, chunk := range file.Chunks {
				referenced[chunk.Digest] = true
			}
		}
	}
	deleted := map[string]bool{}
	for _, key := range keys {
		raw, err := readAll(ctx, store, key)
		if err != nil {
			return err
		}
		var entry trashed
		if err := json.Unmarshal(raw, &entry); err != nil {
			return fmt.Errorf("%s: %w", key, err)
		}
		if now.Sub(entry.TrashedAt) < policy.Grace {
			continue
		}
		for _, file := range entry.Manifest.Files {
			for _, chunk := range file.Chunks {
				if referenced[chunk.Digest] || deleted[chunk.Digest] {
					continue
				}
				chunkObject, err := chunkKey(chunk.Digest)
				if err != nil {
					return err
				}
				if err := store.Delete(ctx, chunkObject); err != nil {
					return err
				}
				deleted[chunk.Digest] = true
				report.RemovedChunks++
				report.FreedBytes += chunk.Size
			}
		}
		if err := store.Delete(ctx, key); err != nil {
			return err
		}
	}
	return nil
}
