package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/guest"
	"rlworld/fleet/internal/snapshot"
)

// containerAgent plays the guest agent inside a dev container: the token, then one JSON request per connection.
type containerAgent struct {
	mu       sync.Mutex
	listener net.Listener
	token    string
	methods  []string
}

func (agent *containerAgent) setToken(token string) {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	agent.token = token
}

func (agent *containerAgent) close() {
	agent.listener.Close()
}

func (agent *containerAgent) serve() {
	for {
		connection, err := agent.listener.Accept()
		if err != nil {
			return
		}
		agent.answer(connection)
	}
}

func (agent *containerAgent) answer(connection net.Conn) {
	defer connection.Close()
	reader := bufio.NewReader(connection)
	greeting, err := reader.ReadString('\n')
	agent.mu.Lock()
	expected := "AUTH " + agent.token + "\n"
	agent.mu.Unlock()
	if err != nil || agent.token == "" || greeting != expected {
		return
	}
	connection.Write([]byte("OK\n"))
	line, err := reader.ReadBytes('\n')
	if err != nil {
		return
	}
	var request struct {
		ID     int64  `json:"id"`
		Method string `json:"method"`
	}
	json.Unmarshal(line, &request)
	agent.mu.Lock()
	agent.methods = append(agent.methods, request.Method)
	agent.mu.Unlock()
	results := map[string]any{
		"prepare":     map[string]any{},
		"act":         map[string]any{},
		"screen_hash": "steady",
		"observe": map[string]any{"step_index": 0, "screenshot_size": []int{1280, 800},
			"screenshot_data": base64.StdEncoding.EncodeToString([]byte("webp frame"))},
		"extract": map[string]any{"files": map[string]any{"report.txt": "done"}},
	}
	reply, _ := json.Marshal(map[string]any{"id": request.ID, "result": results[request.Method]})
	connection.Write(append(reply, '\n'))
}

// fakeDockerCLI hands each run's token to the agent and answers port lookups with the agent's address.
type fakeDockerCLI struct {
	mu    sync.Mutex
	agent *containerAgent
	calls []string
}

func (cli *fakeDockerCLI) Output(_ context.Context, argv []string) ([]byte, error) {
	cli.mu.Lock()
	cli.calls = append(cli.calls, argv[1]+" "+argv[len(argv)-1])
	cli.mu.Unlock()
	if argv[1] == "run" {
		for _, argument := range argv {
			if token, found := strings.CutPrefix(argument, "RLWORLD_AGENT_TOKEN="); found {
				cli.agent.setToken(token)
			}
		}
	}
	if argv[1] == "port" {
		return []byte(cli.agent.listener.Addr().String() + "\n"), nil
	}
	return nil, nil
}

// refusingCommands records and fails host commands; docker hosts must run none.
type refusingCommands struct {
	mu       sync.Mutex
	commands []string
}

func (runner *refusingCommands) Run(_ context.Context, argv []string) error {
	runner.mu.Lock()
	defer runner.mu.Unlock()
	runner.commands = append(runner.commands, strings.Join(argv, " "))
	return errors.New("docker hosts make no networks or overlays")
}

func writeConfig(t *testing.T, payload map[string]any) string {
	t.Helper()
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "fleetd.json")
	if err := os.WriteFile(path, encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func laptopConfig(t *testing.T) map[string]any {
	return map[string]any{
		"listen": "127.0.0.1:8700", "driver": "docker",
		"host":     map[string]any{"id": "laptop", "pool": "linux_dev", "cpu_millicores": 8000, "memory_mib": 16384},
		"capacity": 4, "state_dir": t.TempDir(), "object_store_dir": t.TempDir(),
		"docker": map[string]any{"ready_timeout_seconds": 5},
	}
}

func postJSON(t *testing.T, url string, body any) map[string]any {
	t.Helper()
	encoded, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	response, err := http.Post(url, "application/json", bytes.NewReader(encoded))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode >= 300 {
		t.Fatalf("POST %s: %d", url, response.StatusCode)
	}
	decoded := map[string]any{}
	json.NewDecoder(response.Body).Decode(&decoded)
	return decoded
}

func TestConfigDefaultsAndValidation(t *testing.T) {
	loaded, err := loadConfig(writeConfig(t, laptopConfig(t)))
	if err != nil {
		t.Fatal(err)
	}
	if loaded.LeaseTTLSeconds != 300 || loaded.GuestTimeoutSeconds != 30 {
		t.Fatalf("defaults: %+v", loaded)
	}
	_, err = loadConfig(writeConfig(t, map[string]any{"driver": "hyperv", "capacity": 0}))
	for _, problem := range []string{"listen is required", `driver "hyperv"`, "host needs an id",
		"capacity must be positive", "state_dir and object_store_dir"} {
		if err == nil || !strings.Contains(err.Error(), problem) {
			t.Fatalf("missing %q in %v", problem, err)
		}
	}
	unknown := laptopConfig(t)
	unknown["drvier"] = "docker"
	if _, err := loadConfig(writeConfig(t, unknown)); err == nil || !strings.Contains(err.Error(), "drvier") {
		t.Fatalf("unknown fields must fail: %v", err)
	}
}

func TestTheLaptopExampleConfigLoads(t *testing.T) {
	loaded, err := loadConfig(filepath.Join("..", "..", "..", "examples", "fleetd", "laptop.json"))
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Driver != "docker" || loaded.Host.Pool != "linux" || loaded.Docker.ReadyTimeoutSeconds != 120 {
		t.Fatalf("example: %+v", loaded)
	}
}

// lineAgent plays a guest end that answers one JSON request per connection with result, after an optional
// Firecracker vsock CONNECT; a CONNECT to the VNC relay's port gets a banner instead.
type lineAgent struct {
	mu        sync.Mutex
	listener  net.Listener
	directory string
	vsock     bool
	result    any
	greetings []string
}

func startLineAgent(t *testing.T, directory, socketPath string, vsock bool, result any) *lineAgent {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(socketPath), 0o700); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		os.RemoveAll(directory)
		t.Fatal(err)
	}
	agent := &lineAgent{listener: listener, directory: directory, vsock: vsock, result: result}
	t.Cleanup(agent.close)
	go agent.serve()
	return agent
}

func (agent *lineAgent) close() {
	agent.listener.Close()
	os.RemoveAll(agent.directory)
}

func (agent *lineAgent) serve() {
	for {
		connection, err := agent.listener.Accept()
		if err != nil {
			return
		}
		agent.answer(connection)
	}
}

func (agent *lineAgent) answer(connection net.Conn) {
	defer connection.Close()
	reader := bufio.NewReader(connection)
	if agent.vsock {
		greeting, err := reader.ReadString('\n')
		if err != nil {
			return
		}
		agent.mu.Lock()
		agent.greetings = append(agent.greetings, strings.TrimSpace(greeting))
		agent.mu.Unlock()
		if greeting == fmt.Sprintf("CONNECT %d\n", guestVNCPort) {
			connection.Write([]byte("OK 1\nRFB 003.008\n"))
			return
		}
		connection.Write([]byte("OK 1\n"))
	}
	line, err := reader.ReadBytes('\n')
	if err != nil {
		return
	}
	var request struct {
		ID int64 `json:"id"`
	}
	json.Unmarshal(line, &request)
	reply, _ := json.Marshal(map[string]any{"id": request.ID, "result": agent.result})
	connection.Write(append(reply, '\n'))
}

func TestFirecrackerHostsReachGuestsOverVsock(t *testing.T) {
	loaded, err := loadConfig(writeConfig(t, laptopConfig(t)))
	if err != nil {
		t.Fatal(err)
	}
	chroot, err := os.MkdirTemp("/tmp", "jail")
	if err != nil {
		t.Fatal(err)
	}
	loaded.Driver = "firecracker"
	loaded.Firecracker.ChrootBaseDir = chroot
	loaded.Firecracker.FirecrackerPath = "/usr/bin/firecracker"
	agent := startLineAgent(t, chroot, filepath.Join(chroot, "firecracker", "env_1", "root", "v.sock"), true,
		"steady")
	driver, channel := newDriver(loaded, tools{}, guest.NewFilesystemFrames(t.TempDir()))
	if driver.Name() != "firecracker" {
		t.Fatalf("driver %s", driver.Name())
	}
	if hash, err := channel.ScreenHash(context.Background(), "env_1"); err != nil || hash != "steady" {
		t.Fatalf("screen hash over vsock: %q %v", hash, err)
	}
	connection, reader, err := channel.DialVNC(context.Background(), "env_1")
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	if banner, err := reader.ReadString('\n'); err != nil || banner != "RFB 003.008\n" {
		t.Fatalf("vnc banner: %q %v", banner, err)
	}
	agent.mu.Lock()
	defer agent.mu.Unlock()
	if !slices.Equal(agent.greetings, []string{"CONNECT 5005", "CONNECT 5901"}) {
		t.Fatalf("vsock ports: %v", agent.greetings)
	}
}

func windowsHostConfig(t *testing.T, runDir string) map[string]any {
	payload := laptopConfig(t)
	payload["driver"] = "qemu_templated"
	payload["host"] = map[string]any{"id": "kvm_win_1", "pool": "windows", "cpu_millicores": 96000,
		"memory_mib": 393216, "licenses": []string{"office_ltsc"}}
	payload["qemu"] = map[string]any{"qemu_path": "/usr/bin/qemu-system-x86_64", "run_dir": runDir,
		"machine_type": "pc-q35-9.0", "cpu_model": "fleet_baseline"}
	return payload
}

func TestQEMUHostsPinTheirMachineAndBindSnapshotsToIt(t *testing.T) {
	missing := windowsHostConfig(t, t.TempDir())
	missing["qemu"] = map[string]any{"qemu_path": "/usr/bin/qemu-system-x86_64"}
	if _, err := loadConfig(writeConfig(t, missing)); err == nil ||
		!strings.Contains(err.Error(), "qemu needs qemu_path, run_dir, machine_type and cpu_model") {
		t.Fatalf("a qemu host without its machine: %v", err)
	}
	store := map[string]any{"store_dir": t.TempDir(), "cache_dir": t.TempDir(), "vmm_version": "9.0.2",
		"machine_type": "pc-q35-8.2", "cpu_model": "fleet_baseline"}
	mismatched := windowsHostConfig(t, t.TempDir())
	mismatched["snapshots"] = store
	if _, err := loadConfig(writeConfig(t, mismatched)); err == nil ||
		!strings.Contains(err.Error(), "snapshots must bind the machine type and CPU model qemu runs") {
		t.Fatalf("snapshots bound to another machine: %v", err)
	}
	store["machine_type"] = "pc-q35-9.0"
	bound := windowsHostConfig(t, t.TempDir())
	bound["snapshots"] = store
	loaded, err := loadConfig(writeConfig(t, bound))
	if err != nil {
		t.Fatal(err)
	}
	settings, enabled := loaded.snapshotSettings()
	want := snapshot.Compatibility{Pool: "windows", VMM: "qemu_templated", VMMVersion: "9.0.2",
		MachineType: "pc-q35-9.0", CPUModel: "fleet_baseline"}
	if !enabled || settings.Compatibility != want {
		t.Fatalf("windows snapshot binding: %+v", settings.Compatibility)
	}
}

func TestQEMUHostsReachWindowsGuestsThroughTheirHelper(t *testing.T) {
	directory, err := os.MkdirTemp("/tmp", "qemu")
	if err != nil {
		t.Fatal(err)
	}
	runDir := filepath.Join(directory, "run")
	loaded, err := loadConfig(writeConfig(t, windowsHostConfig(t, runDir)))
	if err != nil {
		os.RemoveAll(directory)
		t.Fatal(err)
	}
	_, err = newFleet(loaded, tools{commands: &refusingCommands{}})
	info, statErr := os.Stat(runDir)
	startLineAgent(t, directory, filepath.Join(runDir, "env_1.helper"), false, map[string]any{"excel": map[string]any{}})
	if err != nil || statErr != nil || !info.IsDir() {
		t.Fatalf("a qemu fleet makes its run dir: %v %v", err, statErr)
	}
	driver, channel := newDriver(loaded, tools{}, guest.NewFilesystemFrames(t.TempDir()))
	if _, isWindows := channel.(*guest.WindowsChannel); !isWindows || driver.Name() != "qemu_templated" {
		t.Fatalf("driver %s channel %T", driver.Name(), channel)
	}
	state, err := channel.Extract(context.Background(), "env_1", []string{"excel"})
	if err != nil || string(state) != `{"excel":{}}` {
		t.Fatalf("extract through the helper: %s %v", state, err)
	}
}

func TestLaptopLoopRunsAnEpisodeInADockerContainer(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	agent := &containerAgent{listener: listener}
	t.Cleanup(agent.close)
	go agent.serve()
	cli := &fakeDockerCLI{agent: agent}
	commands := &refusingCommands{}
	loaded, err := loadConfig(writeConfig(t, laptopConfig(t)))
	if err != nil {
		t.Fatal(err)
	}
	built, err := newFleet(loaded, tools{commands: commands, docker: cli})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(built.handler)
	t.Cleanup(server.Close)
	leased := postJSON(t, server.URL+"/v1/leases", map[string]any{
		"run_id": "run_1", "tenant": "default", "pool": "linux_dev", "priority": "eval", "endpoint": "policy",
		"snapshot":   map[string]any{"digest": "sha256:dev", "base_disk_path": "rlworld-dev/noble_browsers_v1:dev"},
		"memory_mib": 2048, "cpu_millicores": 2000, "vcpus": 2, "group_size": 1,
		"observation_kinds": []string{"screenshot"},
	})
	leaseID := leased["leases"].([]any)[0].(map[string]any)["lease_id"].(string)
	reset := postJSON(t, server.URL+"/v1/leases/"+leaseID+"/reset", map[string]any{})
	screenshot := reset["observation"].(map[string]any)["screenshot"].(map[string]any)
	digest := screenshot["sha256"].(string)
	stored, err := os.ReadFile(filepath.Join(loaded.ObjectStoreDir, "blobs", digest[:2], digest))
	if err != nil || string(stored) != "webp frame" {
		t.Fatalf("frame in the object store: %q %v", stored, err)
	}
	postJSON(t, server.URL+"/v1/leases/"+leaseID+"/step",
		map[string]any{"action": map[string]any{"name": "click", "x": 10, "y": 20}})
	finished := postJSON(t, server.URL+"/v1/leases/"+leaseID+"/finish", map[string]any{"apps": []string{"files"}})
	if finished["final_state"].(map[string]any)["files"].(map[string]any)["report.txt"] != "done" {
		t.Fatalf("finish: %v", finished)
	}
	request, err := http.NewRequest(http.MethodDelete, server.URL+"/v1/leases/"+leaseID, nil)
	if err != nil {
		t.Fatal(err)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil || response.StatusCode != http.StatusNoContent {
		t.Fatalf("close: %v %v", response, err)
	}
	response.Body.Close()
	cli.mu.Lock()
	calls := slices.Clone(cli.calls)
	cli.mu.Unlock()
	resetStart := slices.Index(calls, "rm rlworld-env_00000002")
	if calls[1] != "run rlworld-dev/noble_browsers_v1:dev" || resetStart < 0 ||
		calls[resetStart-1] != "rm rlworld-env_00000001" || calls[len(calls)-1] != "rm rlworld-env_00000002" {
		t.Fatalf("reset restores a fresh container and close removes it: %v", calls)
	}
	if !slices.Contains(calls, "unpause rlworld-env_00000002") {
		t.Fatalf("turns must thaw the container: %v", calls)
	}
	agent.mu.Lock()
	methods := slices.Clone(agent.methods)
	agent.mu.Unlock()
	for _, method := range []string{"prepare", "screen_hash", "observe", "act", "extract"} {
		if !slices.Contains(methods, method) {
			t.Fatalf("agent never got %s: %v", method, methods)
		}
	}
	if len(commands.commands) != 0 {
		t.Fatalf("docker hosts run no host commands: %v", commands.commands)
	}
	if len(built.node.Envs()) != 0 {
		t.Fatalf("closing the lease destroys the env: %v", built.node.Envs())
	}
}

func TestShutdownDestroysEveryEnv(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	agent := &containerAgent{listener: listener}
	t.Cleanup(agent.close)
	go agent.serve()
	cli := &fakeDockerCLI{agent: agent}
	loaded, err := loadConfig(writeConfig(t, laptopConfig(t)))
	if err != nil {
		t.Fatal(err)
	}
	built, err := newFleet(loaded, tools{commands: &refusingCommands{}, docker: cli})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(built.handler)
	t.Cleanup(server.Close)
	postJSON(t, server.URL+"/v1/leases", map[string]any{
		"run_id": "run_1", "tenant": "default", "pool": "linux_dev", "priority": "eval",
		"snapshot":   map[string]any{"digest": "sha256:dev", "base_disk_path": "image"},
		"memory_mib": 1024, "cpu_millicores": 1000, "group_size": 2,
	})
	if len(built.node.Envs()) != 2 {
		t.Fatalf("envs: %v", built.node.Envs())
	}
	if err := built.destroyAll(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(built.node.Envs()) != 0 {
		t.Fatalf("shutdown leaves envs: %v", built.node.Envs())
	}
}

func firecrackerSnapshotConfig(t *testing.T) map[string]any {
	payload := laptopConfig(t)
	payload["driver"] = "firecracker"
	payload["host"] = map[string]any{"id": "kvm_1", "pool": "linux", "cpu_millicores": 96000, "memory_mib": 393216}
	payload["firecracker"] = map[string]any{"jailer_path": "/usr/bin/jailer", "firecracker_path": "/usr/bin/firecracker",
		"chroot_base_dir": t.TempDir(), "uid": 1000, "gid": 1000, "cpu_template_path": "/etc/rlworld/baseline.json"}
	payload["snapshots"] = map[string]any{"store_dir": t.TempDir(), "cache_dir": t.TempDir(), "vmm_version": "1.12.0",
		"cpu_model": "fleet_baseline", "lazy_memory": true, "retention_days": 30, "grace_hours": 2,
		"slow_world_seconds": 7.5}
	return payload
}

func TestSnapshotStoresNeedAVMPoolAndItsBinding(t *testing.T) {
	docker := laptopConfig(t)
	docker["snapshots"] = map[string]any{"store_dir": t.TempDir(), "cache_dir": t.TempDir(), "vmm_version": "1",
		"cpu_model": "x"}
	if _, err := loadConfig(writeConfig(t, docker)); err == nil || !strings.Contains(err.Error(), "docker images") {
		t.Fatalf("a snapshot store on the docker backend: %v", err)
	}
	unbound := firecrackerSnapshotConfig(t)
	unbound["snapshots"] = map[string]any{"store_dir": t.TempDir()}
	if _, err := loadConfig(writeConfig(t, unbound)); err == nil || !strings.Contains(err.Error(), "cpu_model") {
		t.Fatalf("a snapshot store without its binding: %v", err)
	}
	loaded, err := loadConfig(writeConfig(t, firecrackerSnapshotConfig(t)))
	if err != nil {
		t.Fatal(err)
	}
	settings, enabled := loaded.snapshotSettings()
	want := snapshot.Compatibility{Pool: "linux", VMM: "firecracker", VMMVersion: "1.12.0", CPUModel: "fleet_baseline"}
	if !enabled || settings.Compatibility != want || settings.Retention.MinAge != 30*24*time.Hour ||
		settings.Retention.Grace != 2*time.Hour || settings.WorkDir != loaded.Snapshots.CacheDir ||
		settings.SlowWorld != 7500*time.Millisecond {
		t.Fatalf("settings: %+v", settings)
	}
	if _, enabled := (config{}).snapshotSettings(); enabled {
		t.Fatal("no store dir means no snapshot store")
	}
}

func TestAFirecrackerHostServesItsSnapshotStore(t *testing.T) {
	payload := firecrackerSnapshotConfig(t)
	loaded, err := loadConfig(writeConfig(t, payload))
	if err != nil {
		t.Fatal(err)
	}
	built, err := newFleet(loaded, tools{commands: &refusingCommands{}})
	if err != nil {
		t.Fatal(err)
	}
	if !built.snapshots {
		t.Fatal("the snapshot store is on")
	}
	server := httptest.NewServer(built.handler)
	t.Cleanup(server.Close)
	response, err := http.Get(server.URL + "/v1/snapshots")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var listed map[string][]any
	if err := json.NewDecoder(response.Body).Decode(&listed); err != nil || response.StatusCode != http.StatusOK ||
		listed["current"] == nil {
		t.Fatalf("list snapshots: %d %v %v", response.StatusCode, listed, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	collectSnapshots(ctx, built.service, time.Millisecond)
}

func TestWarmPoolsAreConfiguredPerHost(t *testing.T) {
	payload := laptopConfig(t)
	payload["warm_pool"] = map[string]any{"per_snapshot": 2, "idle_minutes": 45, "interval_seconds": 3}
	loaded, err := loadConfig(writeConfig(t, payload))
	if err != nil {
		t.Fatal(err)
	}
	if loaded.WarmPool.PerSnapshot != 2 || loaded.warmPeriod() != 3*time.Second {
		t.Fatalf("warm pool config: %+v", loaded.WarmPool)
	}
	if (config{}).warmPeriod() != defaultWarmPeriod {
		t.Fatal("upkeep runs every ten seconds by default")
	}
	built, err := newFleet(loaded, tools{commands: &refusingCommands{}, docker: &fakeDockerCLI{}})
	if err != nil {
		t.Fatal(err)
	}
	if err := built.service.MaintainWarmPools(context.Background()); err != nil {
		t.Fatalf("upkeep with nothing hot yet: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	maintainWarmPools(ctx, built.service, time.Millisecond)
}
