package firecracker

import (
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

// fakeFirecracker plays the jailed process: it serves the API on the socket the driver expects.
type fakeFirecracker struct {
	mu        sync.Mutex
	driver    *Driver
	argv      [][]string
	requests  []recordedRequest
	failPath  string
	processes []*fakeProcess
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
	server := &http.Server{Handler: http.HandlerFunc(fake.serve)}
	go server.Serve(listener)
	process := &fakeProcess{server: server}
	fake.mu.Lock()
	fake.argv = append(fake.argv, argv)
	fake.processes = append(fake.processes, process)
	fake.mu.Unlock()
	return process, nil
}

func (fake *fakeFirecracker) serve(writer http.ResponseWriter, request *http.Request) {
	body, _ := io.ReadAll(request.Body)
	var decoded map[string]any
	_ = json.Unmarshal(body, &decoded)
	fake.mu.Lock()
	fake.requests = append(fake.requests, recordedRequest{Method: request.Method, Path: request.URL.Path, Body: decoded})
	failPath := fake.failPath
	fake.mu.Unlock()
	if request.URL.Path == failPath {
		writer.WriteHeader(http.StatusBadRequest)
		_, _ = writer.Write([]byte(`{"fault_message": "snapshot file is from another vmm version"}`))
		return
	}
	if request.Method == http.MethodGet {
		_, _ = writer.Write([]byte(`{"id": "vm_1", "state": "Running", "vmm_version": "1.9.0"}`))
		return
	}
	writer.WriteHeader(http.StatusNoContent)
}

// directoryRemover deletes a temporary directory when a test ends.
type directoryRemover string

func (directory directoryRemover) remove() {
	os.RemoveAll(string(directory))
}

func newTestDriver(t *testing.T) (*Driver, *fakeFirecracker) {
	t.Helper()
	chrootBase, err := os.MkdirTemp("/tmp", "fc")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(directoryRemover(chrootBase).remove)
	fake := &fakeFirecracker{}
	driver := NewDriver(Config{JailerPath: "/usr/bin/jailer", FirecrackerPath: "/usr/bin/firecracker",
		ChrootBaseDir: chrootBase, UID: 1000, GID: 1000, SocketWaitTimeout: 2 * time.Second}, fake, nil)
	fake.driver = driver
	return driver, fake
}

func restoreSpec(resume bool) vmm.RestoreSpec {
	return vmm.RestoreSpec{VMID: "vm_1", StatePath: "/snapshots/state.bin", MemoryPath: "/snapshots/memory.bin",
		MemoryMiB: 2048, VCPUs: 2, Namespace: "clone00001", Resume: resume}
}

func TestJailerArgs(t *testing.T) {
	driver, _ := newTestDriver(t)
	argv := strings.Join(driver.JailerArgs(restoreSpec(false)), " ")
	for _, fragment := range []string{
		"--id vm_1", "--exec-file /usr/bin/firecracker", "--uid 1000", "--cgroup-version 2",
		"--cgroup cpu.max=200000 100000", "--netns /var/run/netns/clone00001", "-- --api-sock /run/firecracker.socket",
	} {
		if !strings.Contains(argv, fragment) {
			t.Fatalf("jailer argv %q lacks %q", argv, fragment)
		}
	}
}

func TestRestorePauseResumeSnapshotDestroy(t *testing.T) {
	ctx := context.Background()
	driver, fake := newTestDriver(t)
	restored, err := driver.Restore(ctx, restoreSpec(false))
	if err != nil || restored.State != vmm.Paused {
		t.Fatalf("restore: %+v %v", restored, err)
	}
	if err := driver.Resume(ctx, "vm_1"); err != nil {
		t.Fatalf("resume: %v", err)
	}
	snapshot, err := driver.Snapshot(ctx, "vm_1", vmm.Diff, "/snapshots/step7.state", "/snapshots/step7.memory")
	if err != nil || snapshot.Kind != vmm.Diff {
		t.Fatalf("snapshot: %+v %v", snapshot, err)
	}
	gotCalls := []string{}
	for _, request := range fake.requests {
		gotCalls = append(gotCalls, request.Method+" "+request.Path)
	}
	wantCalls := []string{"PUT /snapshot/load", "PATCH /vm", "PATCH /vm", "PUT /snapshot/create"}
	if !slices.Equal(gotCalls, wantCalls) {
		t.Fatalf("api calls %v, want %v", gotCalls, wantCalls)
	}
	loadBody := fake.requests[0].Body
	memoryBackend := loadBody["mem_backend"].(map[string]any)
	if loadBody["resume_vm"] != false || memoryBackend["backend_type"] != "File" ||
		memoryBackend["backend_path"] != "/snapshots/memory.bin" {
		t.Fatalf("load body %v", loadBody)
	}
	if fake.requests[2].Body["state"] != "Paused" || fake.requests[3].Body["snapshot_type"] != "Diff" {
		t.Fatalf("snapshot must pause first and request a Diff: %v", fake.requests)
	}
	jailDirectory := filepath.Dir(driver.JailRoot("vm_1"))
	if err := driver.Destroy(ctx, "vm_1"); err != nil {
		t.Fatalf("destroy: %v", err)
	}
	if !fake.processes[0].killed {
		t.Fatal("destroy must kill the process")
	}
	if _, err := os.Stat(jailDirectory); !os.IsNotExist(err) {
		t.Fatalf("jail directory still exists: %v", err)
	}
	if err := driver.Pause(ctx, "vm_1"); !errors.Is(err, vmm.ErrUnknownVM) {
		t.Fatalf("pause after destroy: %v", err)
	}
}

func TestAFailedLoadKillsTheProcessAndSurfacesTheFault(t *testing.T) {
	driver, fake := newTestDriver(t)
	fake.failPath = "/snapshot/load"
	_, err := driver.Restore(context.Background(), restoreSpec(true))
	var apiError *APIError
	if !errors.As(err, &apiError) || !strings.Contains(apiError.FaultMessage, "another vmm version") {
		t.Fatalf("restore error: %v", err)
	}
	if !fake.processes[0].killed {
		t.Fatal("a failed restore must not leave the process running")
	}
}

func TestInstanceInfo(t *testing.T) {
	driver, fake := newTestDriver(t)
	if _, err := fake.Start(context.Background(), driver.JailerArgs(restoreSpec(false))); err != nil {
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
	if _, err := driver.Restore(context.Background(), restoreSpec(false)); err == nil {
		t.Fatal("expected a socket timeout")
	}
}

type silentRunner struct{}

func (silentRunner) Start(context.Context, []string) (vmm.Process, error) {
	return &fakeProcess{server: &http.Server{}}, nil
}
