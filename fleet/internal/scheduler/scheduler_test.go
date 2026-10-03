package scheduler

import (
	"errors"
	"reflect"
	"testing"
)

var smallVM = Resources{CPUMillicores: 2000, MemoryMiB: 2048}

func host(id, pool string, memoryMiB int) Host {
	return Host{ID: id, Pool: pool, Capacity: Resources{CPUMillicores: 64_000, MemoryMiB: memoryMiB}}
}

func request(count int) Request {
	return Request{SnapshotDigest: "sha256:snap", Pool: "firecracker", PerVM: smallVM, Count: count}
}

func TestPlacementRespectsTheOSPool(t *testing.T) {
	scheduler := New()
	scheduler.UpsertHost(host("windows_host", "qemu_templated", 100_000))
	if _, err := scheduler.Place(request(1)); !errors.Is(err, ErrNoCapacity) {
		t.Fatalf("a firecracker request on a windows only fleet: got %v", err)
	}
}

func TestWarmBeatsCachedBeatsFetch(t *testing.T) {
	scheduler := New()
	scheduler.UpsertHost(host("fetch_host", "firecracker", 100_000))
	scheduler.UpsertHost(host("cached_host", "firecracker", 100_000))
	scheduler.UpsertHost(host("warm_host", "firecracker", 2_048))
	scheduler.MarkSnapshotCached("cached_host", "sha256:snap")
	scheduler.MarkSnapshotCached("warm_host", "sha256:snap")
	scheduler.SetWarmVMs("warm_host", "sha256:snap", 1)
	placements := []string{}
	for range 3 {
		assignments, err := scheduler.Place(request(1))
		if err != nil {
			t.Fatalf("place: %v", err)
		}
		placements = append(placements, assignments[0].HostID)
	}
	if want := []string{"warm_host", "cached_host", "cached_host"}; !reflect.DeepEqual(placements, want) {
		t.Fatalf("placements %v, want %v", placements, want)
	}
}

func TestForksOfOneSnapshotPackTogether(t *testing.T) {
	scheduler := New()
	scheduler.UpsertHost(host("host_a", "firecracker", 100_000))
	scheduler.UpsertHost(host("host_b", "firecracker", 100_000))
	first, _ := scheduler.Place(request(1))
	second, _ := scheduler.Place(request(1))
	if first[0].HostID != second[0].HostID {
		t.Fatalf("forks split across %s and %s; packing keeps shared pages on one host",
			first[0].HostID, second[0].HostID)
	}
}

func TestAGroupStaysOnOneHostWhenItFits(t *testing.T) {
	scheduler := New()
	scheduler.UpsertHost(host("host_a", "firecracker", 16_384))
	scheduler.UpsertHost(host("host_b", "firecracker", 8_192))
	assignments, err := scheduler.Place(request(8))
	if err != nil {
		t.Fatalf("place: %v", err)
	}
	if len(assignments) != 1 || assignments[0].HostID != "host_a" || assignments[0].Count != 8 {
		t.Fatalf("assignments %+v, want all 8 on host_a", assignments)
	}
	if !assignments[0].NeedsFetch {
		t.Fatal("an uncached snapshot must be fetched")
	}
}

func TestAGroupSpreadsWhenNoHostFitsItAndFailsAtomically(t *testing.T) {
	scheduler := New()
	scheduler.UpsertHost(host("host_a", "firecracker", 8_192))
	scheduler.UpsertHost(host("host_b", "firecracker", 8_192))
	assignments, err := scheduler.Place(request(6))
	if err != nil {
		t.Fatalf("place: %v", err)
	}
	total := 0
	for _, assignment := range assignments {
		total += assignment.Count
	}
	if total != 6 || len(assignments) != 2 {
		t.Fatalf("assignments %+v, want 6 VMs over 2 hosts", assignments)
	}
	if _, err := scheduler.Place(request(3)); !errors.Is(err, ErrNoCapacity) {
		t.Fatalf("over capacity: got %v", err)
	}
	if free := scheduler.FreeResources("host_a").MemoryMiB + scheduler.FreeResources("host_b").MemoryMiB; free != 4_096 {
		t.Fatalf("a failed placement leaked reservations: %d MiB free", free)
	}
}

func TestFreeReturnsResources(t *testing.T) {
	scheduler := New()
	scheduler.UpsertHost(host("host_a", "firecracker", 2_048))
	if _, err := scheduler.Place(request(1)); err != nil {
		t.Fatalf("place: %v", err)
	}
	scheduler.Free("host_a", smallVM)
	if _, err := scheduler.Place(request(1)); err != nil {
		t.Fatalf("place after free: %v", err)
	}
}

func TestDrainingHostsTakeNoNewEnvs(t *testing.T) {
	scheduler := New()
	drainingHost := host("host_a", "firecracker", 100_000)
	drainingHost.Draining = true
	scheduler.UpsertHost(drainingHost)
	if _, err := scheduler.Place(request(1)); !errors.Is(err, ErrNoCapacity) {
		t.Fatalf("draining host: got %v", err)
	}
}

func TestHostScopedLicensesPreferHoldersAndRespectSeats(t *testing.T) {
	scheduler := New()
	holder := host("holder", "qemu_templated", 8_192)
	holder.HeldLicenses = map[string]bool{"windows_server_dc": true}
	scheduler.UpsertHost(holder)
	scheduler.UpsertHost(host("big_new_host", "qemu_templated", 100_000))
	windowsRequest := Request{SnapshotDigest: "sha256:win", Pool: "qemu_templated", PerVM: smallVM, Count: 1,
		HostScopedLicenses: []string{"windows_server_dc"}}
	assignments, err := scheduler.Place(windowsRequest)
	if err != nil || assignments[0].HostID != "holder" {
		t.Fatalf("got %+v %v, want the license holder", assignments, err)
	}
	windowsRequest.Count = 4
	if _, err := scheduler.Place(windowsRequest); !errors.Is(err, ErrNoCapacity) {
		t.Fatalf("no free seats and the holder is full: got %v", err)
	}
	windowsRequest.FreeHostLicenseSeat = map[string]int{"windows_server_dc": 1}
	assignments, err = scheduler.Place(windowsRequest)
	if err != nil || assignments[0].HostID != "big_new_host" {
		t.Fatalf("with a free seat: got %+v %v", assignments, err)
	}
}

func TestPrefetchPicksUncachedHostsWithTheMostRoom(t *testing.T) {
	scheduler := New()
	scheduler.UpsertHost(host("cached", "firecracker", 100_000))
	scheduler.UpsertHost(host("roomy", "firecracker", 90_000))
	scheduler.UpsertHost(host("tight", "firecracker", 10_000))
	scheduler.UpsertHost(host("windows", "qemu_templated", 200_000))
	scheduler.MarkSnapshotCached("cached", "sha256:snap")
	if got := scheduler.PlanPrefetch("sha256:snap", "firecracker", 5); !reflect.DeepEqual(got, []string{"roomy", "tight"}) {
		t.Fatalf("prefetch hosts %v", got)
	}
	if got := scheduler.PlanPrefetch("sha256:snap", "firecracker", 1); !reflect.DeepEqual(got, []string{"roomy"}) {
		t.Fatalf("prefetch hosts %v", got)
	}
}

func TestInvalidCountsAreRejected(t *testing.T) {
	if _, err := New().Place(request(0)); err == nil {
		t.Fatal("a zero count must be rejected")
	}
}
