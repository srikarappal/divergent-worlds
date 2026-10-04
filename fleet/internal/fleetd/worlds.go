package fleetd

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/scheduler"
	"rlworld/fleet/internal/snapshot"
)

// DefaultSlowWorld is the design's "more than about 5 s" of world loading that earns a world snapshot.
const DefaultSlowWorld = 5 * time.Second

// worldTrials is how many fresh envs a world is materialized in to see whether it loads the same way each time.
const worldTrials = 2

// WorldPreparation asks how a world should reach envs of a base snapshot: the payload task setup injects to
// materialize it, the apps whose state shows whether two loads agree and the lease shape the envs take.
type WorldPreparation struct {
	World         string          `json:"world"`
	Payload       json.RawMessage `json:"payload"`
	Apps          []string        `json:"apps"`
	Pool          string          `json:"pool"`
	MemoryMiB     int             `json:"memory_mib"`
	CPUMillicores int             `json:"cpu_millicores"`
	VCPUs         int             `json:"vcpus"`
}

// worldTrial is one fresh env the world was materialized into.
type worldTrial struct {
	envID   string
	seconds float64
	state   []byte
}

// PrepareWorld decides once per base snapshot and world whether the world loads at every restore or from a world
// snapshot (DESIGN.md section 3, lifecycle step 2). It materializes the world in two fresh envs of the base;
// when either load takes longer than SlowWorld or their app states differ, the first env is snapshotted with its
// world and every later restore starts there, leaving only the task's own state to inject.
func (service *Service) PrepareWorld(ctx context.Context, baseDigest string, request WorldPreparation) (
	snapshot.WorldDecision, error) {
	settings, err := service.snapshotSettings()
	if err != nil {
		return snapshot.WorldDecision{}, err
	}
	if err := snapshot.CheckDigest(request.World); err != nil || len(request.Payload) == 0 || request.Pool == "" ||
		request.MemoryMiB <= 0 {
		return snapshot.WorldDecision{}, fmt.Errorf("%w: a world preparation names the world digest, payload, pool "+
			"and memory_mib", ErrBadSnapshotRequest)
	}
	if decision, err := snapshot.LookupWorldDecision(ctx, settings.Store, baseDigest, request.World); err == nil &&
		service.stillPublished(ctx, settings, decision) {
		return decision, nil
	}
	base, err := snapshot.LoadManifest(ctx, settings.Store, baseDigest)
	if err != nil {
		return snapshot.WorldDecision{}, err
	}
	perVM := scheduler.Resources{CPUMillicores: request.CPUMillicores, MemoryMiB: request.MemoryMiB}
	hostID, node, err := service.reserveWorldHost(request.Pool, perVM)
	if err != nil {
		return snapshot.WorldDecision{}, err
	}
	defer service.freeWorldHost(hostID, perVM)
	trials, err := service.materializeTrials(ctx, node, base, request)
	defer service.destroyTrials(node, trials)
	if err != nil {
		return snapshot.WorldDecision{}, err
	}
	decision := snapshot.WorldDecision{Base: baseDigest, World: request.World, Deterministic: true,
		DecidedAt: service.now().UTC()}
	slow := false
	for _, trial := range trials {
		decision.MaterializeSeconds = append(decision.MaterializeSeconds, trial.seconds)
		slow = slow || trial.seconds > slowWorld(settings).Seconds()
		decision.Deterministic = decision.Deterministic && bytes.Equal(trial.state, trials[0].state)
	}
	if slow || !decision.Deterministic {
		if decision.Snapshot, err = service.captureWorld(ctx, settings, node, base, request.World,
			trials[0].envID); err != nil {
			return snapshot.WorldDecision{}, err
		}
	}
	return decision, snapshot.RecordWorldDecision(ctx, settings.Store, decision)
}

func slowWorld(settings *SnapshotSettings) time.Duration {
	if settings.SlowWorld > 0 {
		return settings.SlowWorld
	}
	return DefaultSlowWorld
}

// stillPublished is false for a decision whose world snapshot retention has since removed; it is decided again.
func (service *Service) stillPublished(ctx context.Context, settings *SnapshotSettings,
	decision snapshot.WorldDecision) bool {
	if decision.Snapshot == "" {
		return true
	}
	_, err := snapshot.LoadManifest(ctx, settings.Store, decision.Snapshot)
	return err == nil
}

// reserveWorldHost holds room for both trial envs on one pool host.
func (service *Service) reserveWorldHost(pool string, perVM scheduler.Resources) (string, Node, error) {
	for _, hostID := range service.scheduler.HostsInPool(pool) {
		node, found := service.nodes[hostID]
		if found && service.scheduler.ReserveOnHost(hostID, perVM, worldTrials) == nil {
			return hostID, node, nil
		}
	}
	return "", nil, fmt.Errorf("%w: no %s host has room to measure a world", scheduler.ErrNoCapacity, pool)
}

func (service *Service) freeWorldHost(hostID string, perVM scheduler.Resources) {
	for range worldTrials {
		service.scheduler.Free(hostID, perVM)
	}
}

// materializeTrials restores the base twice and times the world's injection into each, keeping its app state.
func (service *Service) materializeTrials(ctx context.Context, node Node, base snapshot.Manifest,
	request WorldPreparation) ([]worldTrial, error) {
	var trials []worldTrial
	for range worldTrials {
		envID := service.newEnvID()
		if _, err := node.Start(ctx, nodeagent.StartRequest{EnvID: envID, SnapshotDigest: base.Digest,
			MemoryMiB: base.MemoryMiB, VCPUs: base.VCPUs}); err != nil {
			return trials, err
		}
		trials = append(trials, worldTrial{envID: envID})
		started := service.now()
		if err := node.Inject(ctx, envID, request.Payload); err != nil {
			return trials, fmt.Errorf("materialize world %s: %w", request.World, err)
		}
		trials[len(trials)-1].seconds = service.now().Sub(started).Seconds()
		state, err := node.Extract(ctx, envID, request.Apps)
		if err != nil {
			return trials, err
		}
		if trials[len(trials)-1].state, err = canonicalJSON(state); err != nil {
			return trials, err
		}
	}
	return trials, nil
}

func (service *Service) destroyTrials(node Node, trials []worldTrial) {
	for _, trial := range trials {
		node.Destroy(context.Background(), trial.envID)
	}
}

// canonicalJSON re-encodes app state with sorted keys so two equal states compare equal byte for byte.
func canonicalJSON(raw json.RawMessage) ([]byte, error) {
	var decoded any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return nil, fmt.Errorf("app state is not JSON: %w", err)
	}
	return json.Marshal(decoded)
}

// captureWorld snapshots the env holding the materialized world and publishes it with its base and world.
func (service *Service) captureWorld(ctx context.Context, settings *SnapshotSettings, node Node,
	base snapshot.Manifest, world, envID string) (string, error) {
	outputDir := filepath.Join(settings.WorkDir, ".world-"+envID)
	defer os.RemoveAll(outputDir)
	captured, err := node.CaptureSnapshot(ctx, envID, outputDir)
	if err != nil {
		return "", fmt.Errorf("capture world %s: %w", world, err)
	}
	draft := snapshot.Manifest{Image: base.Image, ImageDigest: base.ImageDigest, ImageSpec: base.ImageSpec,
		Compatibility: base.Compatibility, MemoryMiB: base.MemoryMiB, VCPUs: base.VCPUs, Base: base.Digest,
		World: world}
	publisher := snapshot.Publisher{Store: settings.Store, ChunkSize: settings.ChunkSize, Now: service.now}
	manifest, err := publisher.Publish(ctx, draft, map[string]string{snapshot.StateFile: captured.StatePath,
		snapshot.MemoryFile: captured.MemoryPath, snapshot.DiskFile: captured.DiskPath})
	if err != nil {
		return "", err
	}
	return manifest.Digest, nil
}
