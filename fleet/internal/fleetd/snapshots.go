package fleetd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"rlworld/fleet/internal/nodeagent"
	"rlworld/fleet/internal/scheduler"
	"rlworld/fleet/internal/snapshot"
)

// ErrNoSnapshots means this fleetd was started without a snapshot store.
var ErrNoSnapshots = errors.New("this fleetd has no snapshot store")

// ErrBadImageBuild means an image build directory cannot be snapshotted on this pool.
var ErrBadImageBuild = errors.New("bad image build")

// ErrBadSnapshotRequest means a build or prefetch request is missing what it needs.
var ErrBadSnapshotRequest = errors.New("bad snapshot request")

// SnapshotSettings turn on the snapshot store: where snapshots live, what this pool binds them to, where builds
// work and how long unpinned snapshots stay.
type SnapshotSettings struct {
	Store         snapshot.ObjectStore
	Compatibility snapshot.Compatibility
	WorkDir       string
	Retention     snapshot.RetentionPolicy
	ChunkSize     int64
	// SlowWorld is how long materializing a world may take before it gets a world snapshot; zero means 5 s.
	SlowWorld time.Duration
}

// SnapshotBuild asks for a base snapshot of an image build, at the memory and vCPUs the pool's leases use.
type SnapshotBuild struct {
	BuildDir  string `json:"build_dir"`
	MemoryMiB int    `json:"memory_mib"`
	VCPUs     int    `json:"vcpus"`
}

// PrefetchRequest says how many envs of a snapshot a scheduled run will hold at once and how big each is.
type PrefetchRequest struct {
	Pool          string `json:"pool"`
	Envs          int    `json:"envs"`
	MemoryMiB     int    `json:"memory_mib"`
	CPUMillicores int    `json:"cpu_millicores"`
	GPUs          int    `json:"gpus"`
}

// imageBuild is what rlworld-build-image records in manifest.json beside its outputs.
type imageBuild struct {
	Image       json.RawMessage        `json:"image"`
	ImageDigest string                 `json:"image_digest"`
	Target      string                 `json:"target"`
	Outputs     map[string]imageOutput `json:"outputs"`
}

type imageOutput struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

// imageFields are the parts of the image spec the builder needs.
type imageFields struct {
	ID   string `json:"id"`
	Pool struct {
		VMM      string `json:"vmm"`
		CPUModel string `json:"cpu_model"`
	} `json:"pool"`
	ReadyProbe struct {
		Window         *string `json:"window"`
		ScreenStableMs int     `json:"screen_stable_ms"`
		TimeoutS       int     `json:"timeout_s"`
	} `json:"ready_probe"`
}

// EnableSnapshots turns on the snapshot routes.
func (service *Service) EnableSnapshots(settings SnapshotSettings) {
	service.mu.Lock()
	defer service.mu.Unlock()
	service.snapshots = &settings
}

func (service *Service) snapshotSettings() (*SnapshotSettings, error) {
	service.mu.Lock()
	defer service.mu.Unlock()
	if service.snapshots == nil {
		return nil, ErrNoSnapshots
	}
	return service.snapshots, nil
}

// MarkCachedSnapshots tells placement which snapshots each node already holds, as fleetd learns at startup.
func (service *Service) MarkCachedSnapshots() error {
	var problems []error
	for hostID, node := range service.nodes {
		digests, err := node.CachedSnapshots()
		problems = append(problems, err)
		for _, digest := range digests {
			service.scheduler.MarkSnapshotCached(hostID, digest)
		}
	}
	return errors.Join(problems...)
}

func fileSHA256(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

// readImageBuild loads an image build and checks every output against the sha256 the build recorded.
func readImageBuild(buildDir string) (imageBuild, imageFields, error) {
	var build imageBuild
	var fields imageFields
	raw, err := os.ReadFile(filepath.Join(buildDir, "manifest.json"))
	if err != nil {
		return build, fields, fmt.Errorf("%w: %v", ErrBadImageBuild, err)
	}
	if err := json.Unmarshal(raw, &build); err != nil {
		return build, fields, fmt.Errorf("%w: manifest.json: %v", ErrBadImageBuild, err)
	}
	if err := json.Unmarshal(build.Image, &fields); err != nil || fields.ID == "" {
		return build, fields, fmt.Errorf("%w: manifest.json has no image spec", ErrBadImageBuild)
	}
	if err := snapshot.CheckDigest(build.ImageDigest); err != nil {
		return build, fields, fmt.Errorf("%w: image digest: %v", ErrBadImageBuild, err)
	}
	if build.Target != "vm" {
		return build, fields, fmt.Errorf("%w: %s is a %q build; snapshots start from vm builds", ErrBadImageBuild,
			fields.ID, build.Target)
	}
	for role, output := range build.Outputs {
		digest, err := fileSHA256(filepath.Join(buildDir, output.Path))
		if err != nil || digest != output.SHA256 {
			return build, fields, fmt.Errorf("%w: the %s output %s does not match its recorded sha256", ErrBadImageBuild,
				role, output.Path)
		}
	}
	return build, fields, nil
}

// buildRequest maps an image build onto the node agent's boot: Firecracker boots a kernel and an ext4 root, QEMU
// a qcow2 disk.
func buildRequest(buildDir string, build imageBuild, fields imageFields, request SnapshotBuild,
	workDir string) (nodeagent.BuildRequest, error) {
	disk, hasDisk := build.Outputs["disk"]
	if rootfs, hasRootfs := build.Outputs["rootfs"]; hasRootfs {
		disk, hasDisk = rootfs, true
	}
	if !hasDisk {
		return nodeagent.BuildRequest{}, fmt.Errorf("%w: %s has no disk output", ErrBadImageBuild, fields.ID)
	}
	buildID := fmt.Sprintf("build_%s_%s", fields.ID, build.ImageDigest[len(build.ImageDigest)-8:])
	boot := nodeagent.BuildRequest{BuildID: buildID, DiskPath: filepath.Join(buildDir, disk.Path),
		MemoryMiB: request.MemoryMiB, VCPUs: request.VCPUs,
		StableFor: time.Duration(fields.ReadyProbe.ScreenStableMs) * time.Millisecond,
		Timeout:   time.Duration(fields.ReadyProbe.TimeoutS) * time.Second,
		OutputDir: filepath.Join(workDir, "."+buildID)}
	if kernel, found := build.Outputs["kernel"]; found {
		boot.KernelPath = filepath.Join(buildDir, kernel.Path)
	}
	if fields.ReadyProbe.Window != nil {
		boot.ReadyWindow = *fields.ReadyProbe.Window
	}
	return boot, nil
}

// BuildSnapshot boots an image build on a host of this pool, waits for its readiness probe, publishes the base
// snapshot's chunks and makes it the snapshot new leases of the image use (DESIGN.md section 3, lifecycle step 1).
func (service *Service) BuildSnapshot(ctx context.Context, request SnapshotBuild) (snapshot.Manifest, error) {
	settings, err := service.snapshotSettings()
	if err != nil {
		return snapshot.Manifest{}, err
	}
	if request.MemoryMiB <= 0 || request.VCPUs <= 0 {
		return snapshot.Manifest{}, fmt.Errorf("%w: memory_mib and vcpus must be what the pool's leases use",
			ErrBadSnapshotRequest)
	}
	build, fields, err := readImageBuild(request.BuildDir)
	if err != nil {
		return snapshot.Manifest{}, err
	}
	binding := settings.Compatibility
	if fields.Pool.VMM != binding.VMM || fields.Pool.CPUModel != binding.CPUModel {
		return snapshot.Manifest{}, fmt.Errorf("%w: %s wants %s with cpu model %s; this pool is %s", ErrBadImageBuild,
			fields.ID, fields.Pool.VMM, fields.Pool.CPUModel, binding.Describe())
	}
	boot, err := buildRequest(request.BuildDir, build, fields, request, settings.WorkDir)
	if err != nil {
		return snapshot.Manifest{}, err
	}
	perVM := scheduler.Resources{MemoryMiB: request.MemoryMiB, CPUMillicores: request.VCPUs * 1000}
	hostID, node, err := service.reserveBuildHost(binding.Pool, perVM)
	if err != nil {
		return snapshot.Manifest{}, err
	}
	defer service.scheduler.Free(hostID, perVM)
	defer os.RemoveAll(boot.OutputDir)
	result, err := node.BuildSnapshot(ctx, boot)
	if err != nil {
		return snapshot.Manifest{}, err
	}
	if binding.VMMVersion != "" && result.VMMVersion != "" && result.VMMVersion != binding.VMMVersion {
		return snapshot.Manifest{}, fmt.Errorf("%w: host %s declares %s %s but runs %s", snapshot.ErrIncompatible,
			hostID, binding.VMM, binding.VMMVersion, result.VMMVersion)
	}
	draft := snapshot.Manifest{Image: fields.ID, ImageDigest: build.ImageDigest, ImageSpec: build.Image,
		Compatibility: binding, MemoryMiB: request.MemoryMiB, VCPUs: request.VCPUs}
	publisher := snapshot.Publisher{Store: settings.Store, ChunkSize: settings.ChunkSize, Now: service.now}
	manifest, err := publisher.Publish(ctx, draft, map[string]string{snapshot.StateFile: result.StatePath,
		snapshot.MemoryFile: result.MemoryPath, snapshot.DiskFile: result.DiskPath})
	if err != nil {
		return snapshot.Manifest{}, err
	}
	if _, err := snapshot.SetCurrent(ctx, settings.Store, manifest, service.now()); err != nil {
		return snapshot.Manifest{}, err
	}
	return manifest, nil
}

// reserveBuildHost holds room for the build VM on the first pool host that has it.
func (service *Service) reserveBuildHost(pool string, perVM scheduler.Resources) (string, Node, error) {
	for _, hostID := range service.scheduler.HostsInPool(pool) {
		node, found := service.nodes[hostID]
		if found && service.scheduler.ReserveOnHost(hostID, perVM, 1) == nil {
			return hostID, node, nil
		}
	}
	return "", nil, fmt.Errorf("%w: no %s host has room to build a snapshot", scheduler.ErrNoCapacity, pool)
}

// PrefetchSnapshot starts pulls onto enough pool hosts for a scheduled run's envs and returns those hosts; each
// host counts for placement once its pull is done. A pull that fails leaves the next lease to pull lazily.
func (service *Service) PrefetchSnapshot(ctx context.Context, digest string, request PrefetchRequest) ([]string,
	error) {
	settings, err := service.snapshotSettings()
	if err != nil {
		return nil, err
	}
	if request.Pool == "" || request.Envs <= 0 || request.MemoryMiB <= 0 {
		return nil, fmt.Errorf("%w: a prefetch names the pool, envs and memory_mib of the run", ErrBadSnapshotRequest)
	}
	if _, err := snapshot.LoadManifest(ctx, settings.Store, digest); err != nil {
		return nil, err
	}
	perVM := scheduler.Resources{CPUMillicores: request.CPUMillicores, MemoryMiB: request.MemoryMiB, GPUs: request.GPUs}
	hostIDs := service.scheduler.PrefetchTargets(digest, request.Pool, perVM, request.Envs)
	for _, hostID := range hostIDs {
		service.prefetches.Add(1)
		go service.prefetchOn(hostID, digest)
	}
	return hostIDs, nil
}

func (service *Service) prefetchOn(hostID, digest string) {
	defer service.prefetches.Done()
	node, found := service.nodes[hostID]
	if found && node.Prefetch(context.Background(), digest) == nil {
		service.scheduler.MarkSnapshotCached(hostID, digest)
	}
}

// WaitForPrefetches blocks until every prefetch started so far has ended.
func (service *Service) WaitForPrefetches() {
	service.prefetches.Wait()
}

// SnapshotDetail is a published snapshot with who pins it.
type SnapshotDetail struct {
	Manifest snapshot.Manifest `json:"manifest"`
	Pins     []snapshot.Pin    `json:"pins"`
}

// Snapshot reads one published snapshot and its pins.
func (service *Service) Snapshot(ctx context.Context, digest string) (SnapshotDetail, error) {
	settings, err := service.snapshotSettings()
	if err != nil {
		return SnapshotDetail{}, err
	}
	manifest, err := snapshot.LoadManifest(ctx, settings.Store, digest)
	if err != nil {
		return SnapshotDetail{}, err
	}
	pins, err := snapshot.Pins(ctx, settings.Store, digest)
	return SnapshotDetail{Manifest: manifest, Pins: pins}, err
}

// CurrentSnapshots lists the snapshot every image uses on every pool.
func (service *Service) CurrentSnapshots(ctx context.Context) ([]snapshot.Current, error) {
	settings, err := service.snapshotSettings()
	if err != nil {
		return nil, err
	}
	return snapshot.ListCurrent(ctx, settings.Store)
}

// CurrentSnapshot is the snapshot new leases of an image use on a pool.
func (service *Service) CurrentSnapshot(ctx context.Context, imageDigest, pool string) (snapshot.Current, error) {
	settings, err := service.snapshotSettings()
	if err != nil {
		return snapshot.Current{}, err
	}
	return snapshot.CurrentSnapshot(ctx, settings.Store, imageDigest, pool)
}

// PinSnapshot keeps a snapshot for a holder such as run:<run id>, eval_report:<run id> or published:<name>.
func (service *Service) PinSnapshot(ctx context.Context, digest, holder string) (snapshot.Pin, error) {
	settings, err := service.snapshotSettings()
	if err != nil {
		return snapshot.Pin{}, err
	}
	return snapshot.AddPin(ctx, settings.Store, digest, holder, service.now())
}

// UnpinSnapshot drops one holder's pin.
func (service *Service) UnpinSnapshot(ctx context.Context, digest, holder string) error {
	settings, err := service.snapshotSettings()
	if err != nil {
		return err
	}
	return snapshot.RemovePin(ctx, settings.Store, digest, holder)
}

// CollectSnapshots applies the retention policy now.
func (service *Service) CollectSnapshots(ctx context.Context) (snapshot.CollectReport, error) {
	settings, err := service.snapshotSettings()
	if err != nil {
		return snapshot.CollectReport{}, err
	}
	return snapshot.Collect(ctx, settings.Store, settings.Retention, service.now())
}
