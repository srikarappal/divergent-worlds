package docker

import (
	"bufio"
	"context"
	"errors"
	"net"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/vmm"
)

// fakeAgent accepts connections like a booted container's agent: the token first, then OK.
type fakeAgent struct {
	mu       sync.Mutex
	listener net.Listener
	token    string
}

func (agent *fakeAgent) setToken(token string) {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	agent.token = token
}

func (agent *fakeAgent) serve() {
	for {
		connection, err := agent.listener.Accept()
		if err != nil {
			return
		}
		agent.answer(connection)
	}
}

func (agent *fakeAgent) answer(connection net.Conn) {
	defer connection.Close()
	greeting, err := bufio.NewReader(connection).ReadString('\n')
	agent.mu.Lock()
	expected := "AUTH " + agent.token + "\n"
	agent.mu.Unlock()
	if err == nil && agent.token != "" && greeting == expected {
		connection.Write([]byte("OK\n"))
	}
}

func (agent *fakeAgent) close() {
	agent.listener.Close()
}

// recordingDocker plays the docker CLI: it records argv and hands the run's token to the fake agent.
type recordingDocker struct {
	mu            sync.Mutex
	calls         [][]string
	agent         *fakeAgent
	failures      map[string]error
	withholdToken bool
}

func (docker *recordingDocker) Output(_ context.Context, argv []string) ([]byte, error) {
	docker.mu.Lock()
	docker.calls = append(docker.calls, argv)
	failure := docker.failures[argv[1]]
	docker.mu.Unlock()
	if failure != nil {
		return nil, failure
	}
	switch argv[1] {
	case "run":
		for _, argument := range argv {
			if token, found := strings.CutPrefix(argument, "RLWORLD_AGENT_TOKEN="); found && !docker.withholdToken {
				docker.agent.setToken(token)
			}
		}
		return []byte("3f2a\n"), nil
	case "port":
		return []byte(docker.agent.listener.Addr().String() + "\n"), nil
	case "commit":
		return []byte("sha256:abc\n"), nil
	}
	return nil, nil
}

func (docker *recordingDocker) subcommands() []string {
	docker.mu.Lock()
	defer docker.mu.Unlock()
	var names []string
	for _, argv := range docker.calls {
		names = append(names, argv[1]+" "+argv[len(argv)-1])
	}
	return names
}

func startFakeAgent(t *testing.T) *fakeAgent {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	agent := &fakeAgent{listener: listener}
	t.Cleanup(agent.close)
	go agent.serve()
	return agent
}

func newTestDriver(docker *recordingDocker, readyTimeout time.Duration) *Driver {
	driver := NewDriver(Config{ReadyTimeout: readyTimeout}, docker)
	driver.pollPeriod = 5 * time.Millisecond
	return driver
}

func TestRestoreStartsABootedPausedContainer(t *testing.T) {
	docker := &recordingDocker{agent: startFakeAgent(t)}
	driver := newTestDriver(docker, 2*time.Second)
	vm, err := driver.Restore(context.Background(), vmm.RestoreSpec{VMID: "env_1",
		DiskOverlayPath: "rlworld-dev/noble:1", MemoryMiB: 2048, VCPUs: 2})
	if err != nil {
		t.Fatal(err)
	}
	if vm.State != vmm.Paused || vm.Driver != "docker" {
		t.Fatalf("restored vm: %+v", vm)
	}
	run := docker.calls[1]
	for _, expected := range []string{"--detach", "--init", "--name", "rlworld-env_1", "127.0.0.1::5005",
		"seccomp=unconfined", "1024m", "--cpus", "2", "--memory", "2048m", "rlworld.env=env_1"} {
		if !slices.Contains(run, expected) {
			t.Fatalf("run argv lacks %q: %v", expected, run)
		}
	}
	if run[len(run)-1] != "rlworld-dev/noble:1" {
		t.Fatalf("image last: %v", run)
	}
	expected := []string{"rm rlworld-env_1", "run rlworld-dev/noble:1", "port 5005/tcp", "exec " + egressRuleset,
		"pause rlworld-env_1"}
	if !slices.Equal(docker.subcommands(), expected) {
		t.Fatalf("docker calls: %v", docker.subcommands())
	}
	firewall := docker.calls[3]
	if !slices.Equal(firewall[:7], []string{"docker", "exec", "--privileged", "--user", "0", "rlworld-env_1",
		"/usr/sbin/nft"}) {
		t.Fatalf("firewall argv: %v", firewall)
	}
	if slices.Contains(run, "--privileged") || slices.Contains(run, "NET_ADMIN") {
		t.Fatalf("the container itself must not be able to change its firewall: %v", run)
	}
	address, token, err := driver.GuestEndpoint("env_1")
	if err != nil || address != docker.agent.listener.Addr().String() || len(token) != 64 {
		t.Fatalf("endpoint: %q %q %v", address, token, err)
	}
}

func TestRestoreThatResumesLeavesTheContainerRunning(t *testing.T) {
	docker := &recordingDocker{agent: startFakeAgent(t)}
	vm, err := newTestDriver(docker, 2*time.Second).Restore(context.Background(),
		vmm.RestoreSpec{VMID: "env_2", DiskOverlayPath: "image", Resume: true})
	if err != nil || vm.State != vmm.Running {
		t.Fatalf("resumed restore: %+v %v", vm, err)
	}
	if slices.Contains(docker.subcommands(), "pause rlworld-env_2") {
		t.Fatalf("resumed restores must not pause: %v", docker.subcommands())
	}
}

func TestContainersThatAllowEgressGetNoFirewall(t *testing.T) {
	docker := &recordingDocker{agent: startFakeAgent(t)}
	driver := NewDriver(Config{ReadyTimeout: 2 * time.Second, AllowEgress: true}, docker)
	_, err := driver.Restore(context.Background(), vmm.RestoreSpec{VMID: "env_6", DiskOverlayPath: "image"})
	if err != nil {
		t.Fatal(err)
	}
	for _, argv := range docker.calls {
		if argv[1] == "exec" {
			t.Fatalf("allow_egress must skip the firewall: %v", docker.subcommands())
		}
	}
}

func TestContainersWhoseFirewallFailsAreRemoved(t *testing.T) {
	docker := &recordingDocker{agent: startFakeAgent(t), failures: map[string]error{"exec": errors.New("nft: not found")}}
	_, err := newTestDriver(docker, 2*time.Second).Restore(context.Background(),
		vmm.RestoreSpec{VMID: "env_7", DiskOverlayPath: "image"})
	if err == nil || !strings.Contains(err.Error(), "block egress: nft: not found") {
		t.Fatalf("restore without a firewall: %v", err)
	}
	calls := docker.subcommands()
	if calls[len(calls)-1] != "rm rlworld-env_7" {
		t.Fatalf("container must be removed: %v", calls)
	}
}

func TestContainersWhoseAgentNeverAnswersAreRemoved(t *testing.T) {
	docker := &recordingDocker{agent: startFakeAgent(t), failures: map[string]error{}, withholdToken: true}
	driver := newTestDriver(docker, 50*time.Millisecond)
	_, err := driver.Restore(context.Background(), vmm.RestoreSpec{VMID: "env_3", DiskOverlayPath: "image"})
	if err == nil || !strings.Contains(err.Error(), "did not answer") {
		t.Fatalf("restore without an agent: %v", err)
	}
	calls := docker.subcommands()
	if calls[len(calls)-1] != "rm rlworld-env_3" {
		t.Fatalf("container must be removed: %v", calls)
	}
	if _, _, err := driver.GuestEndpoint("env_3"); !errors.Is(err, vmm.ErrUnknownVM) {
		t.Fatalf("failed restores are not tracked: %v", err)
	}
	if _, err := driver.Restore(context.Background(), vmm.RestoreSpec{VMID: "env_4"}); err == nil {
		t.Fatal("a restore needs an image")
	}
	docker.failures["run"] = errors.New("no such image")
	_, err = driver.Restore(context.Background(), vmm.RestoreSpec{VMID: "env_5", DiskOverlayPath: "missing"})
	if err == nil || !strings.Contains(err.Error(), "no such image") {
		t.Fatalf("run failure: %v", err)
	}
}

func TestPauseResumeSnapshotAndDestroy(t *testing.T) {
	docker := &recordingDocker{agent: startFakeAgent(t)}
	driver := newTestDriver(docker, 2*time.Second)
	ctx := context.Background()
	if _, err := driver.Restore(ctx, vmm.RestoreSpec{VMID: "env_1", DiskOverlayPath: "image"}); err != nil {
		t.Fatal(err)
	}
	if err := driver.Pause(ctx, "env_1"); err != nil {
		t.Fatal(err)
	}
	if err := driver.Resume(ctx, "env_1"); err != nil {
		t.Fatal(err)
	}
	if err := driver.Resume(ctx, "env_1"); err != nil {
		t.Fatal(err)
	}
	snapshot, err := driver.Snapshot(ctx, "env_1", vmm.Diff, "/checkpoints/env_1_at_42.state", "/checkpoints/x.memory")
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.StatePath != "rlworld-snapshot:env_1_at_42" || snapshot.MemoryPath != "" || snapshot.Kind != vmm.Full {
		t.Fatalf("snapshot: %+v", snapshot)
	}
	if err := driver.Destroy(ctx, "env_1"); err != nil {
		t.Fatal(err)
	}
	expected := []string{"rm rlworld-env_1", "run image", "port 5005/tcp", "exec " + egressRuleset,
		"pause rlworld-env_1", "unpause rlworld-env_1", "pause rlworld-env_1", "commit rlworld-snapshot:env_1_at_42",
		"rm rlworld-env_1"}
	if !slices.Equal(docker.subcommands(), expected) {
		t.Fatalf("docker calls: %v", docker.subcommands())
	}
	if err := driver.Pause(ctx, "env_1"); !errors.Is(err, vmm.ErrUnknownVM) {
		t.Fatalf("destroyed env: %v", err)
	}
}

func TestSnapshotTagsAreValidImageTags(t *testing.T) {
	driver := NewDriver(Config{SnapshotRepository: "dev-snapshots"}, &recordingDocker{})
	if tag := driver.SnapshotTag("env_1", "/c/env 1:at+7.state"); tag != "dev-snapshots:env_1_at_7" {
		t.Fatalf("tag: %q", tag)
	}
	if tag := driver.SnapshotTag("env_1", ""); tag != "dev-snapshots:env_1" {
		t.Fatalf("empty state path: %q", tag)
	}
	longTag := driver.SnapshotTag("env_1", "/c/"+strings.Repeat("a", 200)+".state")
	if len(longTag) != len("dev-snapshots:")+128 {
		t.Fatalf("long tag: %d", len(longTag))
	}
}

func TestExecCommanderFoldsStderrIntoErrors(t *testing.T) {
	output, err := ExecCommander{}.Output(context.Background(), []string{"sh", "-c", "echo booted"})
	if err != nil || string(output) != "booted\n" {
		t.Fatalf("output: %q %v", output, err)
	}
	_, err = ExecCommander{}.Output(context.Background(), []string{"sh", "-c", "echo no such image >&2; exit 3"})
	if err == nil || !strings.Contains(err.Error(), "no such image") || !strings.HasPrefix(err.Error(), "sh -c") {
		t.Fatalf("failure: %v", err)
	}
}
