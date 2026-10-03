package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"

	"rlworld/fleet/internal/guest"
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
	_, err = loadConfig(writeConfig(t, map[string]any{"driver": "qemu_templated", "capacity": 0}))
	for _, problem := range []string{"listen is required", `driver "qemu_templated"`, "host needs an id",
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

func TestFirecrackerHostsReachGuestsOverVsock(t *testing.T) {
	loaded, err := loadConfig(writeConfig(t, laptopConfig(t)))
	if err != nil {
		t.Fatal(err)
	}
	loaded.Driver = "firecracker"
	loaded.Firecracker.ChrootBaseDir = "/srv/jailer"
	loaded.Firecracker.FirecrackerPath = "/usr/bin/firecracker"
	driver, dialer := newDriver(loaded, tools{})
	vsock, isVsock := dialer.(guest.VsockDialer)
	if driver.Name() != "firecracker" || !isVsock || vsock.Port != guestAgentPort {
		t.Fatalf("driver %s dialer %T", driver.Name(), dialer)
	}
	if path := vsock.SocketPath("env_1"); path != "/srv/jailer/firecracker/env_1/root/v.sock" {
		t.Fatalf("vsock path: %s", path)
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
