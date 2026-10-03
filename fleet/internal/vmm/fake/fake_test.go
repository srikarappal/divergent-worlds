package fake

import (
	"context"
	"errors"
	"testing"

	"rlworld/fleet/internal/vmm"
)

func TestFakeDriverLifecycle(t *testing.T) {
	ctx := context.Background()
	driver := New()
	restored, err := driver.Restore(ctx, vmm.RestoreSpec{VMID: "vm_1"})
	if err != nil || restored.State != vmm.Paused {
		t.Fatalf("restore: %+v %v", restored, err)
	}
	if err := driver.Resume(ctx, "vm_1"); err != nil || driver.State("vm_1") != vmm.Running {
		t.Fatalf("resume: %v", err)
	}
	snapshot, err := driver.Snapshot(ctx, "vm_1", vmm.Diff, "state.bin", "memory.bin")
	if err != nil || snapshot.Kind != vmm.Diff || driver.State("vm_1") != vmm.Paused {
		t.Fatalf("snapshot: %+v %v", snapshot, err)
	}
	if err := driver.Destroy(ctx, "vm_1"); err != nil || driver.State("vm_1") != vmm.Destroyed {
		t.Fatalf("destroy: %v", err)
	}
	if err := driver.Pause(ctx, "vm_1"); !errors.Is(err, vmm.ErrUnknownVM) {
		t.Fatalf("pause after destroy: %v", err)
	}
	want := []string{"restore vm_1", "resume vm_1", "snapshot vm_1", "destroy vm_1"}
	if len(driver.Operations) != len(want) {
		t.Fatalf("operations %v, want %v", driver.Operations, want)
	}
}

func TestFakeDriverInjectsFailures(t *testing.T) {
	driver := New()
	driver.FailNext["restore"] = errors.New("snapshot missing")
	if _, err := driver.Restore(context.Background(), vmm.RestoreSpec{VMID: "vm_1"}); err == nil {
		t.Fatal("expected the injected failure")
	}
	if _, err := driver.Restore(context.Background(), vmm.RestoreSpec{VMID: "vm_1"}); err != nil {
		t.Fatalf("failures fire once: %v", err)
	}
}
