package firecracker

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/vmm"
)

type recordedRequest struct {
	Method string
	Path   string
	Body   map[string]any
}

type fakeProcess struct {
	server *http.Server
	killed bool
}

func (process *fakeProcess) PID() int    { return 4242 }
func (process *fakeProcess) Wait() error { return nil }
func (process *fakeProcess) Kill() error {
	process.killed = true
	return process.server.Close()
}

// fakeFirecracker plays the jailed processes: each serves the API on the socket the driver expects.
type fakeFirecracker struct {
	mu        sync.Mutex
	driver    *Driver
	argv      [][]string
	requests  []recordedRequest
	failPath  string
	processes []*fakeProcess
}

// fakeVM is one jailed process; it writes snapshot files where Firecracker would, inside its jail.
type fakeVM struct {
	fake *fakeFirecracker
	vmID string
}

func (fake *fakeFirecracker) Start(_ context.Context, argv []string) (vmm.Process, error) {
	vmID := argv[slices.Index(argv, "--id")+1]
	socketPath := fake.driver.SocketPath(vmID)
	if err := os.MkdirAll(filepath.Dir(socketPath), 0o755); err != nil {
		return nil, err
	}
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, err
	}
	server := &http.Server{Handler: http.HandlerFunc((&fakeVM{fake: fake, vmID: vmID}).serve)}
	go server.Serve(listener)
	process := &fakeProcess{server: server}
	fake.mu.Lock()
	fake.argv = append(fake.argv, argv)
	fake.processes = append(fake.processes, process)
	fake.mu.Unlock()
	return process, nil
}

func (vm *fakeVM) serve(writer http.ResponseWriter, request *http.Request) {
	body, _ := io.ReadAll(request.Body)
	var decoded map[string]any
	json.Unmarshal(body, &decoded)
	fake := vm.fake
	fake.mu.Lock()
	fake.requests = append(fake.requests, recordedRequest{Method: request.Method, Path: request.URL.Path, Body: decoded})
	failPath := fake.failPath
	fake.mu.Unlock()
	if request.URL.Path == failPath {
		writer.WriteHeader(http.StatusBadRequest)
		writer.Write([]byte(`{"fault_message": "snapshot file is from another vmm version"}`))
		return
	}
	if request.URL.Path == "/snapshot/create" {
		root := fake.driver.JailRoot(vm.vmID)
		os.WriteFile(filepath.Join(root, decoded["snapshot_path"].(string)), []byte("device state"), 0o644)
		os.WriteFile(filepath.Join(root, decoded["mem_file_path"].(string)), []byte("guest memory"), 0o644)
	}
	if request.Method == http.MethodGet {
		writer.Write([]byte(`{"id": "vm_1", "state": "Running", "vmm_version": "1.9.0"}`))
		return
	}
	writer.WriteHeader(http.StatusNoContent)
}

func (fake *fakeFirecracker) calls() []string {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	var calls []string
	for _, request := range fake.requests {
		calls = append(calls, request.Method+" "+request.Path)
	}
	return calls
}

func (fake *fakeFirecracker) body(path string) map[string]any {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	for _, request := range fake.requests {
		if request.Path == path {
			return request.Body
		}
	}
	return nil
}

// ownership records what the driver hands to the VM's user instead of changing owners on the test machine.
type ownership struct {
	mu     sync.Mutex
	chowns []string
}

func (owners *ownership) chown(name string, uid, gid int) error {
	owners.mu.Lock()
	defer owners.mu.Unlock()
	owners.chowns = append(owners.chowns, filepath.Base(name))
	return nil
}

// directoryRemover deletes a temporary directory when a test ends.
type directoryRemover string

func (directory directoryRemover) remove() {
	os.RemoveAll(string(directory))
}

// shortTempDir lives under /tmp: unix socket paths are short and jails need the files on one filesystem.
func shortTempDir(t *testing.T, prefix string) string {
	t.Helper()
	directory, err := os.MkdirTemp("/tmp", prefix)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(directoryRemover(directory).remove)
	return directory
}

func newTestDriver(t *testing.T, config Config) (*Driver, *fakeFirecracker, *ownership) {
	t.Helper()
	config.JailerPath, config.FirecrackerPath = "/usr/bin/jailer", "/usr/bin/firecracker"
	config.ChrootBaseDir, config.UID, config.GID = shortTempDir(t, "fc"), 1000, 1000
	config.SocketWaitTimeout = 2 * time.Second
	fake := &fakeFirecracker{}
	driver := NewDriver(config, fake, nil)
	owners := &ownership{}
	driver.chown = owners.chown
	fake.driver = driver
	return driver, fake, owners
}

// snapshotFiles writes a clone's state, memory and overlay as the node agent would have them.
func snapshotFiles(t *testing.T) (string, string, string) {
	t.Helper()
	directory := shortTempDir(t, "snap")
	paths := []string{filepath.Join(directory, "state"), filepath.Join(directory, "memory"),
		filepath.Join(directory, "overlay.disk")}
	for _, path := range paths {
		if err := os.WriteFile(path, []byte(filepath.Base(path)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return paths[0], paths[1], paths[2]
}

func restoreSpec(t *testing.T, resume bool) vmm.RestoreSpec {
	statePath, memoryPath, diskPath := snapshotFiles(t)
	return vmm.RestoreSpec{VMID: "vm_1", StatePath: statePath, MemoryPath: memoryPath, DiskOverlayPath: diskPath,
		MemoryMiB: 2048, VCPUs: 2, Namespace: "clone00001", Resume: resume}
}

func TestJailerArgs(t *testing.T) {
	driver, _, _ := newTestDriver(t, Config{})
	argv := strings.Join(driver.JailerArgs("vm_1", 2, 2048, "clone00001"), " ")
	for _, fragment := range []string{
		"--id vm_1", "--exec-file /usr/bin/firecracker", "--uid 1000", "--cgroup-version 2",
		"--cgroup cpu.max=200000 100000", "--cgroup memory.max=2415919104", "--netns /var/run/netns/clone00001",
		"-- --api-sock /run/firecracker.socket",
	} {
		if !strings.Contains(argv, fragment) {
			t.Fatalf("jailer argv %q lacks %q", argv, fragment)
		}
	}
	if bare := strings.Join(driver.JailerArgs("vm_2", 0, 0, ""), " "); strings.Contains(bare, "--cgroup cpu") ||
		strings.Contains(bare, "--netns") {
		t.Fatalf("no limits or namespace asked: %q", bare)
	}
}

func TestRestoreLinksTheClonesFilesUnderTheNamesTheSnapshotRecorded(t *testing.T) {
	ctx := context.Background()
	driver, fake, owners := newTestDriver(t, Config{})
	spec := restoreSpec(t, false)
	restored, err := driver.Restore(ctx, spec)
	if err != nil || restored.State != vmm.Paused {
		t.Fatalf("restore: %+v %v", restored, err)
	}
	root := driver.JailRoot("vm_1")
	for jailName, hostPath := range map[string]string{"snapshot.state": spec.StatePath,
		"snapshot.memory": spec.MemoryPath, "rootfs.ext4": spec.DiskOverlayPath} {
		linked, err := os.Stat(filepath.Join(root, jailName))
		original, _ := os.Stat(hostPath)
		if err != nil || !os.SameFile(linked, original) {
			t.Fatalf("%s must be a hard link of %s: %v", jailName, hostPath, err)
		}
	}
	if !slices.Equal(owners.chowns, []string{"rootfs.ext4"}) {
		t.Fatalf("only the disk the VM writes changes hands: %v", owners.chowns)
	}
	load := fake.body("/snapshot/load")
	backend := load["mem_backend"].(map[string]any)
	if load["snapshot_path"] != "snapshot.state" || backend["backend_type"] != "File" ||
		backend["backend_path"] != "snapshot.memory" || load["resume_vm"] != false {
		t.Fatalf("load body %v", load)
	}
	if err := driver.Resume(ctx, "vm_1"); err != nil {
		t.Fatalf("resume: %v", err)
	}
	checkpointDir := shortTempDir(t, "ckpt")
	statePath, memoryPath := filepath.Join(checkpointDir, "step7.state"), filepath.Join(checkpointDir, "step7.memory")
	snapshot, err := driver.Snapshot(ctx, "vm_1", vmm.Diff, statePath, memoryPath)
	if err != nil || snapshot.Kind != vmm.Diff || snapshot.StatePath != statePath {
		t.Fatalf("snapshot: %+v %v", snapshot, err)
	}
	if state, _ := os.ReadFile(statePath); string(state) != "device state" {
		t.Fatalf("the checkpoint moves out of the jail: %q", state)
	}
	if _, err := os.Stat(filepath.Join(root, "checkpoint.state")); !os.IsNotExist(err) {
		t.Fatalf("nothing of the checkpoint stays in the jail: %v", err)
	}
	wantCalls := []string{"PUT /snapshot/load", "PATCH /vm", "PATCH /vm", "PUT /snapshot/create"}
	if !slices.Equal(fake.calls(), wantCalls) {
		t.Fatalf("api calls %v, want %v", fake.calls(), wantCalls)
	}
	create := fake.body("/snapshot/create")
	if create["snapshot_type"] != "Diff" || create["snapshot_path"] != "checkpoint.state" {
		t.Fatalf("snapshot must pause first and write inside the jail: %v", create)
	}
	version, err := driver.VMMVersion(ctx, "vm_1")
	if err != nil || version != "1.9.0" {
		t.Fatalf("vmm version: %q %v", version, err)
	}
	jailDirectory := filepath.Dir(root)
	if err := driver.Destroy(ctx, "vm_1"); err != nil {
		t.Fatalf("destroy: %v", err)
	}
	if !fake.processes[0].killed {
		t.Fatal("destroy must kill the process")
	}
	if _, err := os.Stat(jailDirectory); !os.IsNotExist(err) {
		t.Fatalf("jail directory still exists: %v", err)
	}
	if _, err := os.Stat(spec.MemoryPath); err != nil {
		t.Fatalf("removing the jail leaves the cached snapshot: %v", err)
	}
	if err := driver.Pause(ctx, "vm_1"); !errors.Is(err, vmm.ErrUnknownVM) {
		t.Fatalf("pause after destroy: %v", err)
	}
}

func TestLazyMemoryIsServedThroughAPageSocketInsteadOfTheFile(t *testing.T) {
	ctx := context.Background()
	driver, fake, owners := newTestDriver(t, Config{})
	spec := restoreSpec(t, true)
	spec.MemoryPath = ""
	spec.LazyMemory = memoryBytes("guest memory")
	if _, err := driver.Restore(ctx, spec); err != nil {
		t.Fatal(err)
	}
	backend := fake.body("/snapshot/load")["mem_backend"].(map[string]any)
	if backend["backend_type"] != "Uffd" || backend["backend_path"] != "uffd.sock" {
		t.Fatalf("lazy restore backend: %v", backend)
	}
	root := driver.JailRoot("vm_1")
	if _, err := os.Stat(filepath.Join(root, "snapshot.memory")); !os.IsNotExist(err) {
		t.Fatalf("no memory file enters the jail: %v", err)
	}
	if info, err := os.Stat(filepath.Join(root, "uffd.sock")); err != nil || info.Mode()&os.ModeSocket == 0 {
		t.Fatalf("the page socket waits in the jail: %v", err)
	}
	if !slices.Contains(owners.chowns, "uffd.sock") {
		t.Fatalf("the jailed VM user owns the page socket: %v", owners.chowns)
	}
	if err := driver.Destroy(ctx, "vm_1"); err != nil {
		t.Fatalf("destroying stops the page server cleanly: %v", err)
	}
}

func TestFailedRestoresStopTheProcessAndRemoveTheJail(t *testing.T) {
	driver, fake, _ := newTestDriver(t, Config{})
	fake.failPath = "/snapshot/load"
	_, err := driver.Restore(context.Background(), restoreSpec(t, true))
	var apiError *APIError
	if !errors.As(err, &apiError) || !strings.Contains(apiError.FaultMessage, "another vmm version") {
		t.Fatalf("restore error: %v", err)
	}
	if !fake.processes[0].killed {
		t.Fatal("a failed restore must not leave the process running")
	}
	if _, err := os.Stat(filepath.Dir(driver.JailRoot("vm_1"))); !os.IsNotExist(err) {
		t.Fatalf("a failed restore leaves no jail: %v", err)
	}
	fake.failPath = ""
	missing := restoreSpec(t, false)
	missing.VMID = "vm_2"
	missing.StatePath = filepath.Join(filepath.Dir(missing.StatePath), "never_pulled")
	if _, err := driver.Restore(context.Background(), missing); err == nil ||
		!strings.Contains(err.Error(), "share a filesystem") {
		t.Fatalf("a snapshot file that cannot be linked: %v", err)
	}
	if !fake.processes[1].killed {
		t.Fatal("the process stops when its files cannot be linked")
	}
}

func TestBootStartsTheImageWithTheDevicesEveryCloneKeeps(t *testing.T) {
	ctx := context.Background()
	template := filepath.Join(shortTempDir(t, "tpl"), "fleet_baseline.json")
	if err := os.WriteFile(template, []byte(`{"cpuid_modifiers": []}`), 0o644); err != nil {
		t.Fatal(err)
	}
	driver, fake, owners := newTestDriver(t, Config{CPUTemplatePath: template})
	images := shortTempDir(t, "img")
	kernel, disk := filepath.Join(images, "vmlinux"), filepath.Join(images, "rootfs.ext4")
	for _, path := range []string{kernel, disk} {
		if err := os.WriteFile(path, []byte(filepath.Base(path)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	booted, err := driver.Boot(ctx, vmm.BootSpec{VMID: "build_1", KernelPath: kernel, DiskPath: disk, MemoryMiB: 2048,
		VCPUs: 2, Namespace: "clone00007"})
	if err != nil || booted.State != vmm.Running {
		t.Fatalf("boot: %+v %v", booted, err)
	}
	wantCalls := []string{"PUT /machine-config", "PUT /cpu-config", "PUT /boot-source", "PUT /drives/rootfs",
		"PUT /vsock", "PUT /network-interfaces/eth0", "PUT /actions"}
	if !slices.Equal(fake.calls(), wantCalls) {
		t.Fatalf("boot calls %v", fake.calls())
	}
	machine := fake.body("/machine-config")
	if machine["vcpu_count"] != float64(2) || machine["mem_size_mib"] != float64(2048) ||
		machine["track_dirty_pages"] != true {
		t.Fatalf("machine config %v", machine)
	}
	if source := fake.body("/boot-source"); source["kernel_image_path"] != "vmlinux" ||
		source["boot_args"] != DefaultBootArgs {
		t.Fatalf("boot source %v", source)
	}
	if drive := fake.body("/drives/rootfs"); drive["path_on_host"] != "rootfs.ext4" || drive["is_root_device"] != true {
		t.Fatalf("root drive %v", drive)
	}
	if vsock := fake.body("/vsock"); vsock["uds_path"] != "v.sock" || vsock["guest_cid"] != float64(3) {
		t.Fatalf("vsock %v", vsock)
	}
	if nic := fake.body("/network-interfaces/eth0"); nic["host_dev_name"] != "tap0" ||
		nic["guest_mac"] != "06:00:ac:10:00:02" {
		t.Fatalf("nic %v", nic)
	}
	if fake.body("/cpu-config")["cpuid_modifiers"] == nil {
		t.Fatal("the pool's cpu template is applied before boot")
	}
	if !slices.Equal(owners.chowns, []string{"rootfs.ext4"}) {
		t.Fatalf("the VM user owns the disk it writes: %v", owners.chowns)
	}
	if err := driver.Destroy(ctx, "build_1"); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(disk); err != nil {
		t.Fatalf("the build's disk outlives the jail: %v", err)
	}
	broken := filepath.Join(filepath.Dir(template), "broken.json")
	if err := os.WriteFile(broken, []byte("{"), 0o644); err != nil {
		t.Fatal(err)
	}
	badDriver, badFake, _ := newTestDriver(t, Config{CPUTemplatePath: broken})
	if _, err := badDriver.Boot(ctx, vmm.BootSpec{VMID: "build_2", KernelPath: kernel, DiskPath: disk}); err == nil {
		t.Fatal("a cpu template that is not JSON must stop the boot")
	}
	if len(badFake.processes) != 0 {
		t.Fatal("no jailer starts for a bad cpu template")
	}
}

func TestInstanceInfo(t *testing.T) {
	driver, fake, _ := newTestDriver(t, Config{})
	if _, err := fake.Start(context.Background(), driver.JailerArgs("vm_1", 2, 2048, "")); err != nil {
		t.Fatal(err)
	}
	info, err := NewAPIClient(driver.SocketPath("vm_1")).InstanceInfo(context.Background())
	if err != nil || info.VMMVersion != "1.9.0" {
		t.Fatalf("instance info: %+v %v", info, err)
	}
}

func TestMissingSocketTimesOut(t *testing.T) {
	driver := NewDriver(Config{ChrootBaseDir: "/tmp/does_not_exist", FirecrackerPath: "/usr/bin/firecracker",
		SocketWaitTimeout: 30 * time.Millisecond}, silentRunner{}, nil)
	if _, err := driver.Restore(context.Background(), vmm.RestoreSpec{VMID: "vm_1"}); err == nil {
		t.Fatal("expected a socket timeout")
	}
}

type silentRunner struct{}

func (silentRunner) Start(context.Context, []string) (vmm.Process, error) {
	return &fakeProcess{server: &http.Server{}}, nil
}

// memoryBytes is a page source over bytes held in memory.
type memoryBytes string

func (memory memoryBytes) ReadAt(buffer []byte, offset int64) (int, error) {
	return bytes.NewReader([]byte(memory)).ReadAt(buffer, offset)
}

func (memory memoryBytes) Size() int64 {
	return int64(len(memory))
}

// fakeFaults feeds page faults and records what the server maps.
type fakeFaults struct {
	mu      sync.Mutex
	events  chan faultEvent
	copies  map[uint64][]byte
	zeroed  []addressRange
	closed  bool
	copyErr error
}

func newFakeFaults(events ...faultEvent) *fakeFaults {
	queue := make(chan faultEvent, len(events)+1)
	for _, event := range events {
		queue <- event
	}
	return &fakeFaults{events: queue, copies: map[uint64][]byte{}}
}

func (faults *fakeFaults) next() (faultEvent, error) {
	event, open := <-faults.events
	if !open {
		return faultEvent{}, io.EOF
	}
	return event, nil
}

func (faults *fakeFaults) copyPage(address uint64, page []byte) error {
	faults.mu.Lock()
	defer faults.mu.Unlock()
	faults.copies[address] = slices.Clone(page)
	return faults.copyErr
}

func (faults *fakeFaults) zeroRange(address, length uint64) error {
	faults.mu.Lock()
	defer faults.mu.Unlock()
	faults.zeroed = append(faults.zeroed, addressRange{start: address, end: address + length})
	return nil
}

func (faults *fakeFaults) close() error {
	faults.mu.Lock()
	defer faults.mu.Unlock()
	if !faults.closed {
		faults.closed = true
		close(faults.events)
	}
	return nil
}

func TestThePageServerCopiesEachFaultingPageFromTheMemoryFile(t *testing.T) {
	memory := memoryBytes(strings.Repeat("a", 16) + strings.Repeat("b", 16) + "cc")
	regions := []uffdRegion{{BaseHostVirtAddr: 0x1000, Size: 32, Offset: 0, PageSize: 16},
		{BaseHostVirtAddr: 0x9000, Size: 16, Offset: 32, PageSize: 16}}
	faults := newFakeFaults(
		faultEvent{kind: uffdEventPagefault, address: 0x1013},
		faultEvent{kind: uffdEventPagefault, address: 0x9004},
		faultEvent{kind: uffdEventRemove, start: 0x1000, end: 0x1010},
		faultEvent{kind: uffdEventPagefault, address: 0x1001},
		faultEvent{kind: 0x13},
	)
	server := &pageServer{source: memory, regions: regions}
	faults.close()
	if err := server.serve(faults); !errors.Is(err, io.EOF) {
		t.Fatalf("serving ends when the userfaultfd closes: %v", err)
	}
	if string(faults.copies[0x1010]) != strings.Repeat("b", 16) {
		t.Fatalf("the second page of the first region: %q", faults.copies[0x1010])
	}
	if got := faults.copies[0x9000]; string(got) != "cc"+strings.Repeat("\x00", 14) {
		t.Fatalf("the page past the end of the file reads as zeros: %q", got)
	}
	if len(faults.zeroed) != 1 || faults.zeroed[0] != (addressRange{start: 0x1000, end: 0x1010}) {
		t.Fatalf("a page the guest gave back is zeroed, not read: %+v", faults.zeroed)
	}
	if _, copied := faults.copies[0x1000]; copied {
		t.Fatal("a removed page never gets its old contents")
	}
	outside := newFakeFaults(faultEvent{kind: uffdEventPagefault, address: 0x5000})
	if err := server.serve(outside); err == nil || !strings.Contains(err.Error(), "outside guest memory") {
		t.Fatalf("a fault outside every region: %v", err)
	}
	failing := newFakeFaults(faultEvent{kind: uffdEventPagefault, address: 0x1010})
	failing.copyErr = errors.New("EFAULT")
	if err := server.serve(failing); err == nil || err.Error() != "EFAULT" {
		t.Fatalf("a failed copy stops the server: %v", err)
	}
}

func TestPageRegionsComeInEitherFirecrackerFormat(t *testing.T) {
	regions, err := parseRegions([]byte(`[{"base_host_virt_addr": 4096, "size": 8192, "offset": 0, "page_size": 4096},
		{"base_host_virt_addr": 65536, "size": 2097152, "offset": 8192, "page_size_kib": 2048},
		{"base_host_virt_addr": 9999999, "size": 4096, "offset": 4096}]`))
	if err != nil || len(regions) != 3 {
		t.Fatalf("regions: %+v %v", regions, err)
	}
	if regions[0].pageSize() != 4096 || regions[1].pageSize() != 2<<20 || regions[2].pageSize() != defaultPageSize {
		t.Fatalf("page sizes: %+v", regions)
	}
	for _, body := range []string{`[]`, `{`, `[{"base_host_virt_addr": 0, "size": 1, "page_size": 3000}]`} {
		if _, err := parseRegions([]byte(body)); err == nil {
			t.Fatalf("%s must be refused", body)
		}
	}
}

func TestStoppingAWaitingPageServerIsNotAFailure(t *testing.T) {
	socketPath := filepath.Join(shortTempDir(t, "uffd"), "uffd.sock")
	server, err := listenForPages(socketPath, memoryBytes("pages"))
	if err != nil {
		t.Fatal(err)
	}
	go server.run()
	time.Sleep(10 * time.Millisecond)
	if err := server.stop(); err != nil {
		t.Fatalf("stopping before firecracker connected: %v", err)
	}
	faults := newFakeFaults()
	serving := newPageServer(memoryBytes("pages"), &closedListener{})
	serving.faults = faults
	close(serving.done)
	if err := serving.stop(); err != nil || !faults.closed {
		t.Fatalf("stopping closes the userfaultfd: %v %v", err, faults.closed)
	}
}

// closedListener stands in for a listener whose connection has been taken.
type closedListener struct{}

func (*closedListener) Accept() (net.Conn, error) { return nil, net.ErrClosed }
func (*closedListener) Close() error              { return nil }
func (*closedListener) Addr() net.Addr            { return &net.UnixAddr{Name: "uffd.sock", Net: "unix"} }
