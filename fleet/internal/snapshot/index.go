package snapshot

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"
)

var poolPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9_.-]*$`)

// Current is the snapshot new leases of an image use on one pool. Building a new one for the same image, as a
// VMM upgrade or a new host SKU forces, replaces it; that change is what an image bump is.
type Current struct {
	Image       string    `json:"image"`
	ImageDigest string    `json:"image_digest"`
	Pool        string    `json:"pool"`
	Snapshot    string    `json:"snapshot"`
	UpdatedAt   time.Time `json:"updated_at"`
}

func currentKey(imageDigest, pool string) (string, error) {
	hexPart, err := digestHex(imageDigest)
	if err != nil {
		return "", err
	}
	if !poolPattern.MatchString(pool) {
		return "", fmt.Errorf("pool %q is not a lowercase name", pool)
	}
	return "images/" + hexPart + "/" + pool + ".json", nil
}

// SetCurrent makes a published snapshot the one new leases of its image use on its pool.
func SetCurrent(ctx context.Context, store ObjectStore, manifest Manifest, now time.Time) (Current, error) {
	key, err := currentKey(manifest.ImageDigest, manifest.Compatibility.Pool)
	if err != nil {
		return Current{}, err
	}
	current := Current{Image: manifest.Image, ImageDigest: manifest.ImageDigest, Pool: manifest.Compatibility.Pool,
		Snapshot: manifest.Digest, UpdatedAt: now.UTC()}
	encoded, err := json.Marshal(current)
	if err != nil {
		return Current{}, err
	}
	return current, store.Put(ctx, key, bytes.NewReader(encoded))
}

// CurrentSnapshot is the snapshot new leases of an image use on a pool.
func CurrentSnapshot(ctx context.Context, store ObjectStore, imageDigest, pool string) (Current, error) {
	key, err := currentKey(imageDigest, pool)
	if err != nil {
		return Current{}, err
	}
	raw, err := readAll(ctx, store, key)
	if err != nil {
		return Current{}, err
	}
	var current Current
	return current, json.Unmarshal(raw, &current)
}

// ListCurrent lists the current snapshot of every image on every pool.
func ListCurrent(ctx context.Context, store ObjectStore) ([]Current, error) {
	keys, err := store.List(ctx, "images/")
	if err != nil {
		return nil, err
	}
	var currents []Current
	for _, key := range keys {
		if !strings.HasSuffix(key, ".json") {
			continue
		}
		raw, err := readAll(ctx, store, key)
		if err != nil {
			return nil, err
		}
		var current Current
		if err := json.Unmarshal(raw, &current); err != nil {
			return nil, fmt.Errorf("%s: %w", key, err)
		}
		currents = append(currents, current)
	}
	return currents, nil
}
