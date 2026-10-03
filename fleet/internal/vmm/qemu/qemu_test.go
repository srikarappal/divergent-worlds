package qemu

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/vmm"
)

// fakeQMPServer answers like QEMU: a greeting, an event before some replies and errors for "fail".
type fakeQMPServer struct {
	directory string
	listener  net.Listener
}

func (server *fakeQMPServer) serve() {
	connection, err := server.listener.Accept()
	if err != nil {
		return
	}
	defer connection.Close()
	connection.Write([]byte(`{"QMP": {"version": {"qemu": {"major": 9}}, "capabilities": []}}` + "\r\n"))
	scanner := bufio.NewScanner(connection)
	for scanner.Scan() {
		var command struct {
			Execute string `json:"execute"`
		}
		json.Unmarshal(scanner.Bytes(), &command)
		switch command.Execute {
		case "query-status":
			connection.Write([]byte(`{"event": "RESUME", "timestamp": {"seconds": 1}}` + "\r\n"))
			connection.Write([]byte(`{"return": {"status": "running", "running": true}}` + "\r\n"))
		case "fail":
			connection.Write([]byte(`{"error": {"class": "GenericError", "desc": "nope"}}` + "\r\n"))
		default:
			connection.Write([]byte(`{"return": {}}` + "\r\n"))
		}
	}
}

func (server *fakeQMPServer) close() {
	server.listener.Close()
	os.RemoveAll(server.directory)
}

// serveFakeQMP starts a fake QMP server on a short unix socket path and returns that path.
func serveFakeQMP(t *testing.T) string {
	t.Helper()
	directory, err := os.MkdirTemp("/tmp", "qmp")
	if err != nil {
		t.Fatal(err)
	}
	socketPath := filepath.Join(directory, "vm.qmp")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		os.RemoveAll(directory)
		t.Fatal(err)
	}
	server := &fakeQMPServer{directory: directory, listener: listener}
	t.Cleanup(server.close)
	go server.serve()
	return socketPath
}

func TestQMPClientNegotiatesAndSkipsEvents(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, err := DialQMP(ctx, serveFakeQMP(t))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer client.Close()
	reply, err := client.Execute(ctx, "query-status", nil)
	if err != nil || !strings.Contains(string(reply), `"running"`) {
		t.Fatalf("query-status: %s %v", reply, err)
	}
	_, err = client.Execute(ctx, "fail", nil)
	var qmpError *QMPError
	if !errors.As(err, &qmpError) || qmpError.Description != "nope" {
		t.Fatalf("error reply: %v", err)
	}
}

type fakeQMP struct {
	mu              sync.Mutex
	commands        []string
	arguments       []any
	migrationPolls  int
	migrationStatus []string
	closed          bool
}

func (fake *fakeQMP) Execute(_ context.Context, command string, arguments any) (json.RawMessage, error) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	fake.commands = append(fake.commands, command)
	fake.arguments = append(fake.arguments, arguments)
	if command == "query-migrate" {
		status := fake.migrationStatus[min(fake.migrationPolls, len(fake.migrationStatus)-1)]
		fake.migrationPolls++
		return json.RawMessage(`{"status": "` + status + `"}`), nil
	}
	return json.RawMessage(`{}`), nil
}

func (fake *fakeQMP) dial(context.Context, string) (QMP, error) {
	return fake, nil
}

func (fake *fakeQMP) Close() error {
	fake.closed = true
	return nil
}

type fakeProcess struct{ killed bool }

func (process *fakeProcess) PID() int    { return 7 }
func (process *fakeProcess) Wait() error { return nil }
func (process *fakeProcess) Kill() error {
	process.killed = true
	return nil
}

type recordingRunner struct {
	argv    []string
	process *fakeProcess
}

func (runner *recordingRunner) Start(_ context.Context, argv []string) (vmm.Process, error) {
	runner.argv = argv
	runner.process = &fakeProcess{}
	return runner.process, nil
}

func newTestDriver(channel *fakeQMP) (*Driver, *recordingRunner) {
	runner := &recordingRunner{}
	driver := NewDriver(Config{QEMUPath: "qemu-system-x86_64", MachineType: "pc-q35-9.0", CPUModel: "fleet_baseline",
		RunDir: "/run/rlworld", MigrationPollDelay: time.Millisecond}, runner, channel.dial)
	return driver, runner
}

func windowsSpec(resume bool) vmm.RestoreSpec {
	return vmm.RestoreSpec{VMID: "win_1", StatePath: "/templates/win.state", MemoryPath: "/templates/win.ram",
		DiskOverlayPath: "/overlays/win_1.qcow2", MemoryMiB: 4096, VCPUs: 2, Namespace: "clone00002", Resume: resume}
}

func TestTemplatingArgsMapRAMPrivateAndWaitForDeviceState(t *testing.T) {
	driver, _ := newTestDriver(&fakeQMP{})
	argv := strings.Join(driver.TemplatingArgs(windowsSpec(false)), " ")
	for _, fragment := range []string{
		"ip netns exec clone00002 qemu-system-x86_64",
		"-machine pc-q35-9.0,accel=kvm,memory-backend=guest_ram",
		"memory-backend-file,id=guest_ram,size=4096M,mem-path=/templates/win.ram,share=off",
		"-device usb-tablet", "-incoming defer", "-qmp unix:/run/rlworld/win_1.qmp,server=on,wait=off",
	} {
		if !strings.Contains(argv, fragment) {
			t.Fatalf("qemu argv %q lacks %q", argv, fragment)
		}
	}
}

func TestRestoreLoadsDeviceStateThenRuns(t *testing.T) {
	channel := &fakeQMP{migrationStatus: []string{"active", "active", "completed"}}
	driver, _ := newTestDriver(channel)
	restored, err := driver.Restore(context.Background(), windowsSpec(true))
	if err != nil || restored.State != vmm.Running {
		t.Fatalf("restore: %+v %v", restored, err)
	}
	want := []string{"migrate-set-capabilities", "migrate-incoming", "query-migrate", "query-migrate", "query-migrate", "cont"}
	if !slices.Equal(channel.commands, want) {
		t.Fatalf("commands %v, want %v", channel.commands, want)
	}
	capabilities, _ := json.Marshal(channel.arguments[0])
	if !strings.Contains(string(capabilities), `"x-ignore-shared"`) {
		t.Fatalf("templating needs x-ignore-shared: %s", capabilities)
	}
	if uri := channel.arguments[1].(map[string]any)["uri"]; uri != "file:/templates/win.state" {
		t.Fatalf("incoming uri %v", uri)
	}
}

func TestAFailedMigrationKillsQEMU(t *testing.T) {
	channel := &fakeQMP{migrationStatus: []string{"failed"}}
	driver, runner := newTestDriver(channel)
	if _, err := driver.Restore(context.Background(), windowsSpec(false)); err == nil {
		t.Fatal("expected the migration failure")
	}
	if !runner.process.killed || !channel.closed {
		t.Fatal("a failed restore must kill qemu and close qmp")
	}
}

func TestSnapshotScreenInputAndDestroy(t *testing.T) {
	ctx := context.Background()
	channel := &fakeQMP{migrationStatus: []string{"completed"}}
	driver, runner := newTestDriver(channel)
	if _, err := driver.Restore(ctx, windowsSpec(false)); err != nil {
		t.Fatal(err)
	}
	channel.commands = nil
	channel.arguments = nil
	if _, err := driver.Snapshot(ctx, "win_1", vmm.Diff, "/checkpoints/a", ""); err == nil {
		t.Fatal("diff checkpoints are not supported by templating")
	}
	if _, err := driver.Snapshot(ctx, "win_1", vmm.Full, "/checkpoints/a", ""); err != nil {
		t.Fatal(err)
	}
	if err := driver.CaptureScreen(ctx, "win_1", "/frames/1.png"); err != nil {
		t.Fatal(err)
	}
	if err := driver.SendKeys(ctx, "win_1", []string{"ctrl", "s"}); err != nil {
		t.Fatal(err)
	}
	if err := driver.Destroy(ctx, "win_1"); err != nil {
		t.Fatal(err)
	}
	want := []string{"stop", "migrate", "query-migrate", "screendump", "input-send-event", "quit"}
	if !slices.Equal(channel.commands, want) {
		t.Fatalf("commands %v, want %v", channel.commands, want)
	}
	if !runner.process.killed {
		t.Fatal("destroy must kill the process")
	}
}

func TestKeyAndPointerEvents(t *testing.T) {
	events, err := KeyEvents([]string{"ctrl", "shift", "page_down"})
	if err != nil {
		t.Fatal(err)
	}
	order := []string{}
	for _, event := range events {
		data := event["data"].(map[string]any)
		key := data["key"].(map[string]any)["data"].(string)
		if data["down"].(bool) {
			order = append(order, "down "+key)
		} else {
			order = append(order, "up "+key)
		}
	}
	want := []string{"down ctrl", "down shift", "down pgdn", "up pgdn", "up shift", "up ctrl"}
	if !slices.Equal(order, want) {
		t.Fatalf("key order %v, want %v", order, want)
	}
	if _, err := KeyEvents([]string{"hyperspace"}); err == nil {
		t.Fatal("unknown keys must be rejected")
	}
	if AbsoluteAxis(0, 1920) != 0 || AbsoluteAxis(1919, 1920) != 0x7fff || AbsoluteAxis(5000, 1920) != 0x7fff {
		t.Fatal("pointer axes must span 0 to 0x7fff and clamp")
	}
	if got := AbsoluteAxis(960, 1920); got != 960*0x7fff/1919 {
		t.Fatalf("midpoint axis %d", got)
	}
	pointer := PointerEvents(10, 20, 1280, 800, "left", true)
	if len(pointer) != 3 || pointer[2]["type"] != "btn" {
		t.Fatalf("pointer events %v", pointer)
	}
}
