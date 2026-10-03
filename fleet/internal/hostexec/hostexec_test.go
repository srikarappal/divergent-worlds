package hostexec

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestCommandsFoldOutputIntoErrors(t *testing.T) {
	if err := (Commands{}).Run(context.Background(), []string{"sh", "-c", "exit 0"}); err != nil {
		t.Fatal(err)
	}
	err := (Commands{}).Run(context.Background(), []string{"sh", "-c", "echo netns exists; exit 2"})
	if err == nil || !strings.Contains(err.Error(), "netns exists") {
		t.Fatalf("failure: %v", err)
	}
	if err := (Commands{}).Run(context.Background(), nil); err == nil {
		t.Fatal("an empty command must fail")
	}
}

func TestProcessesOutliveTheContextThatStartedThem(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	started, err := (Processes{}).Start(ctx, []string{"sleep", "30"})
	if err != nil {
		t.Fatal(err)
	}
	cancel()
	time.Sleep(50 * time.Millisecond)
	if started.PID() <= 0 {
		t.Fatalf("pid: %d", started.PID())
	}
	if err := started.Kill(); err != nil {
		t.Fatalf("the process must still be running after its context ended: %v", err)
	}
	if err := started.Wait(); err == nil || !strings.Contains(err.Error(), "killed") {
		t.Fatalf("wait after kill: %v", err)
	}
	if _, err := (Processes{}).Start(context.Background(), []string{"/no/such/vmm"}); err == nil {
		t.Fatal("a missing binary must fail to start")
	}
}
