package nodeagent

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"rlworld/fleet/internal/clock"
	"rlworld/fleet/internal/vmm"
	"rlworld/fleet/internal/vmm/fake"
)

type recordingCommands struct {
	mu        sync.Mutex
	commands  []string
	failMatch string
}

func (runner *recordingCommands) Run(_ context.Context, argv []string) error {
	runner.mu.Lock()
	defer runner.mu.Unlock()
	command := strings.Join(argv, " ")
	runner.commands = append(runner.commands, command)
	if runner.failMatch != "" && strings.Contains(command, runner.failMatch) {
		return errors.New("command failed")
	}
	return nil
}

// scriptedGuest returns screen hashes from a script, one per poll, then repeats the last one.
type scriptedGuest struct {
	hashes   []string
	polls    int
	actions  []string
	injected []string
	failAct  bool
}

func (guest *scriptedGuest) Act(_ context.Context, _ string, action json.RawMessage) error {
	guest.actions = append(guest.actions, string(action))
	if guest.failAct {
		return errors.New("guest agent unreachable")
	}
	return nil
}

func (guest *scriptedGuest) ScreenHash(context.Context, string) (string, error) {
	hash := guest.hashes[min(guest.polls, len(guest.hashes)-1)]
	guest.polls++
	return hash, nil
}

func (guest *scriptedGuest) Observe(context.Context, string) (json.RawMessage, error) {
	return json.RawMessage(`{"step_index": 1}`), nil
}

func (guest *scriptedGuest) Inject(_ context.Context, _ string, payload json.RawMessage) error {
	guest.injected = append(guest.injected, string(payload))
	return nil
}

func (guest *scriptedGuest) Extract(_ context.Context, _ string, apps []string) (json.RawMessage, error) {
	return json.RawMessage(`{"apps": "` + strings.Join(apps, ",") + `"}`), nil
}

func newTestAgent(guest *scriptedGuest) (*Agent, *fake.Driver, *recordingCommands, *clock.Fake) {
	driver := fake.New()
	commands := &recordingCommands{}
	fakeClock := clock.NewFake(time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC))
	agent := New(Config{HostID: "host_a", OverlayDir: "/var/lib/rlworld/overlays"}, driver, guest, commands, fakeClock,
		fakeClock.Advance)
	return agent, driver, commands, fakeClock
}

func startRequest(envID string) StartRequest {
	return StartRequest{EnvID: envID, StatePath: "/snapshots/base.state", MemoryPath: "/snapshots/base.memory",
		BaseDiskPath: "/images/base.ext4", MemoryMiB: 2048, VCPUs: 2}
}

func TestStartWiresNetworkOverlayThenRestores(t *testing.T) {
	agent, driver, commands, _ := newTestAgent(&scriptedGuest{hashes: []string{"a"}})
	first, err := agent.Start(context.Background(), startRequest("env_1"))
	if err != nil {
		t.Fatalf("start: %v", err)
	}
	second, _ := agent.Start(context.Background(), startRequest("env_2"))
	if first.Namespace == second.Namespace || first.CloneAddress == second.CloneAddress {
		t.Fatalf("clones must not share network slots: %+v %+v", first, second)
	}
	if first.State != vmm.Paused {
		t.Fatalf("envs start paused unless asked to resume: %v", first.State)
	}
	if !slices.Contains(commands.commands, "cp --reflink=always /images/base.ext4 /var/lib/rlworld/overlays/env_1.disk") {
		t.Fatalf("no overlay command in %v", commands.commands)
	}
	if driver.Operations[0] != "restore env_1" {
		t.Fatalf("driver operations %v", driver.Operations)
	}
}

func TestAFailedSetupUndoesItselfAndFreesTheSlot(t *testing.T) {
	agent, driver, commands, _ := newTestAgent(&scriptedGuest{hashes: []string{"a"}})
	commands.failMatch = "tuntap"
	if _, err := agent.Start(context.Background(), startRequest("env_1")); err == nil {
		t.Fatal("expected the setup failure")
	}
	if len(driver.Operations) != 0 {
		t.Fatalf("restore must not run after a failed setup: %v", driver.Operations)
	}
	if !slices.Contains(commands.commands, "ip netns del clone00000") {
		t.Fatalf("no teardown in %v", commands.commands)
	}
	commands.failMatch = ""
	info, err := agent.Start(context.Background(), startRequest("env_1"))
	if err != nil || info.Namespace != "clone00001" {
		t.Fatalf("start after failure: %+v %v", info, err)
	}
}

func TestDestroyTearsEverythingDownAndReusesTheSlot(t *testing.T) {
	agent, driver, commands, _ := newTestAgent(&scriptedGuest{hashes: []string{"a"}})
	info, _ := agent.Start(context.Background(), startRequest("env_1"))
	if err := agent.Destroy(context.Background(), "env_1"); err != nil {
		t.Fatalf("destroy: %v", err)
	}
	if driver.State("env_1") != vmm.Destroyed || len(agent.Envs()) != 0 {
		t.Fatal("env still present after destroy")
	}
	if !slices.Contains(commands.commands, "rm -f /var/lib/rlworld/overlays/env_1.disk") {
		t.Fatalf("overlay not removed: %v", commands.commands)
	}
	if !slices.Contains(commands.commands, "ip netns del "+info.Namespace) {
		t.Fatalf("namespace not removed: %v", commands.commands)
	}
	if err := agent.Destroy(context.Background(), "env_1"); !errors.Is(err, ErrUnknownEnv) {
		t.Fatalf("double destroy: %v", err)
	}
}

func TestStepRunsOnlyWhileActingAndSettling(t *testing.T) {
	guest := &scriptedGuest{hashes: []string{"loading", "loading_more", "ready", "ready", "ready", "ready", "ready"}}
	agent, driver, _, _ := newTestAgent(guest)
	agent.Start(context.Background(), startRequest("env_1"))
	result, err := agent.Step(context.Background(), "env_1", json.RawMessage(`{"name": "click", "x": 5, "y": 6}`))
	if err != nil {
		t.Fatalf("step: %v", err)
	}
	if result.SettleCapped || result.SettleDuration != 600*time.Millisecond {
		t.Fatalf("settle %v capped %v, want 600ms uncapped", result.SettleDuration, result.SettleCapped)
	}
	if string(result.Observation) != `{"step_index": 1}` || result.EnvDuration != 600*time.Millisecond {
		t.Fatalf("result %+v", result)
	}
	if want := []string{"restore env_1", "resume env_1", "pause env_1"}; !slices.Equal(driver.Operations, want) {
		t.Fatalf("operations %v, want %v", driver.Operations, want)
	}
	if driver.State("env_1") != vmm.Paused {
		t.Fatal("the env must be paused between steps")
	}
}

func TestSettlingGivesUpAtTheCap(t *testing.T) {
	animated := make([]string, 100)
	for index := range animated {
		animated[index] = string(rune('a' + index%26))
	}
	agent, _, _, _ := newTestAgent(&scriptedGuest{hashes: animated})
	agent.Start(context.Background(), startRequest("env_1"))
	result, err := agent.Step(context.Background(), "env_1", json.RawMessage(`{"name": "wait"}`))
	if err != nil || !result.SettleCapped || result.SettleDuration != 3*time.Second {
		t.Fatalf("settle %v capped %v err %v", result.SettleDuration, result.SettleCapped, err)
	}
}

func TestAFailedActionStillPausesTheEnv(t *testing.T) {
	agent, driver, _, _ := newTestAgent(&scriptedGuest{hashes: []string{"a"}, failAct: true})
	agent.Start(context.Background(), startRequest("env_1"))
	if _, err := agent.Step(context.Background(), "env_1", json.RawMessage(`{}`)); err == nil {
		t.Fatal("expected the action failure")
	}
	if driver.State("env_1") != vmm.Paused {
		t.Fatal("a failed step must leave the env paused")
	}
}

func TestForkCheckpointsAndStartsClonesFromIt(t *testing.T) {
	agent, driver, commands, _ := newTestAgent(&scriptedGuest{hashes: []string{"a"}})
	request := startRequest("env_1")
	request.Resume = true
	agent.Start(context.Background(), request)
	forks, err := agent.Fork(context.Background(), "env_1", 3, "/var/lib/rlworld/checkpoints")
	if err != nil {
		t.Fatalf("fork: %v", err)
	}
	if len(forks) != 3 || forks[2].EnvID != "env_1_fork_2" || forks[0].State != vmm.Paused {
		t.Fatalf("forks %+v", forks)
	}
	if driver.State("env_1") != vmm.Running {
		t.Fatal("a running source must keep running after the fork")
	}
	checkpointCopies := 0
	for _, command := range commands.commands {
		if strings.HasPrefix(command, "cp --reflink=always /var/lib/rlworld/overlays/env_1.disk") {
			checkpointCopies++
		}
	}
	if checkpointCopies != 1 {
		t.Fatalf("the source disk must be copied once for the checkpoint: %v", commands.commands)
	}
	if !slices.Contains(driver.Operations, "snapshot env_1") {
		t.Fatalf("no checkpoint in %v", driver.Operations)
	}
}

func TestInjectReachesTheGuest(t *testing.T) {
	guest := &scriptedGuest{hashes: []string{"a"}}
	agent, _, _, _ := newTestAgent(guest)
	agent.Start(context.Background(), startRequest("env_1"))
	if err := agent.Inject(context.Background(), "env_1", json.RawMessage(`{"browser_cookies": {}}`)); err != nil {
		t.Fatal(err)
	}
	if len(guest.injected) != 1 {
		t.Fatalf("injected %v", guest.injected)
	}
	if err := agent.Inject(context.Background(), "env_9", json.RawMessage(`{}`)); !errors.Is(err, ErrUnknownEnv) {
		t.Fatalf("inject into an unknown env: %v", err)
	}
}

func TestObserveCheckpointAndExtract(t *testing.T) {
	guest := &scriptedGuest{hashes: []string{"a"}}
	agent, driver, _, _ := newTestAgent(guest)
	agent.Start(context.Background(), startRequest("env_1"))
	if _, err := agent.Observe(context.Background(), "env_1"); err != nil || len(guest.actions) != 0 {
		t.Fatalf("observe must not act: %v %v", err, guest.actions)
	}
	snapshot, err := agent.Checkpoint(context.Background(), "env_1", "/checkpoints/a.state", "/checkpoints/a.memory")
	if err != nil || snapshot.Kind != vmm.Full || driver.State("env_1") != vmm.Paused {
		t.Fatalf("checkpoint: %+v %v", snapshot, err)
	}
	state, err := agent.Extract(context.Background(), "env_1", []string{"mail", "files"})
	if err != nil || string(state) != `{"apps": "mail,files"}` {
		t.Fatalf("extract: %s %v", state, err)
	}
}

func TestQEMUEnvsGetQcow2Overlays(t *testing.T) {
	argv := strings.Join(OverlayCommand("qemu_templated", "/images/win.qcow2", "/overlays/win_1.disk"), " ")
	if argv != "qemu-img create -f qcow2 -F qcow2 -b /images/win.qcow2 /overlays/win_1.disk" {
		t.Fatalf("overlay command %q", argv)
	}
}
