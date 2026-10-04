package snapshot

import (
	"bytes"
	"context"
	"encoding/json"
	"time"
)

// WorldDecision records whether a world loads into envs of a base snapshot at every restore or from a world
// snapshot (DESIGN.md section 3, lifecycle step 2): a world that takes too long to load or loads differently each
// time is snapshotted once materialized. Decided once per base snapshot and world.
type WorldDecision struct {
	Base               string    `json:"base"`
	World              string    `json:"world"`
	Snapshot           string    `json:"snapshot,omitempty"`
	MaterializeSeconds []float64 `json:"materialize_seconds"`
	Deterministic      bool      `json:"deterministic"`
	DecidedAt          time.Time `json:"decided_at"`
}

func worldKey(baseDigest, worldDigest string) (string, error) {
	baseHex, err := digestHex(baseDigest)
	if err != nil {
		return "", err
	}
	worldHex, err := digestHex(worldDigest)
	if err != nil {
		return "", err
	}
	return "worlds/" + baseHex + "/" + worldHex + ".json", nil
}

// RecordWorldDecision keeps how a world loads on a base snapshot.
func RecordWorldDecision(ctx context.Context, store ObjectStore, decision WorldDecision) error {
	key, err := worldKey(decision.Base, decision.World)
	if err != nil {
		return err
	}
	encoded, err := json.Marshal(decision)
	if err != nil {
		return err
	}
	return store.Put(ctx, key, bytes.NewReader(encoded))
}

// LookupWorldDecision reads how a world loads on a base snapshot, if that was decided.
func LookupWorldDecision(ctx context.Context, store ObjectStore, baseDigest, worldDigest string) (WorldDecision,
	error) {
	key, err := worldKey(baseDigest, worldDigest)
	if err != nil {
		return WorldDecision{}, err
	}
	raw, err := readAll(ctx, store, key)
	if err != nil {
		return WorldDecision{}, err
	}
	var decision WorldDecision
	return decision, json.Unmarshal(raw, &decision)
}
