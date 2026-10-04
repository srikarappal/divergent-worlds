package firecracker

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"

	"rlworld/fleet/internal/vmm"
)

// The userfaultfd events the page server acts on (linux/userfaultfd.h).
const (
	uffdEventPagefault = 0x12
	uffdEventRemove    = 0x15
	defaultPageSize    = 4096
)

// uffdRegion is one guest memory region as Firecracker hands it over when it loads a snapshot with the Uffd
// backend: where Firecracker mapped it and where its pages start in the memory file. Older Firecracker versions
// name the page size in KiB.
type uffdRegion struct {
	BaseHostVirtAddr uint64 `json:"base_host_virt_addr"`
	Size             uint64 `json:"size"`
	Offset           uint64 `json:"offset"`
	PageSize         uint64 `json:"page_size"`
	PageSizeKiB      uint64 `json:"page_size_kib"`
}

func (region uffdRegion) pageSize() uint64 {
	switch {
	case region.PageSize > 0:
		return region.PageSize
	case region.PageSizeKiB > 0:
		return region.PageSizeKiB * 1024
	default:
		return defaultPageSize
	}
}

func (region uffdRegion) contains(address uint64) bool {
	return address >= region.BaseHostVirtAddr && address-region.BaseHostVirtAddr < region.Size
}

func parseRegions(body []byte) ([]uffdRegion, error) {
	var regions []uffdRegion
	if err := json.Unmarshal(body, &regions); err != nil {
		return nil, fmt.Errorf("firecracker's guest memory regions: %w", err)
	}
	if len(regions) == 0 {
		return nil, errors.New("firecracker sent no guest memory regions")
	}
	for _, region := range regions {
		if size := region.pageSize(); size&(size-1) != 0 {
			return nil, fmt.Errorf("page size %d is not a power of two", size)
		}
	}
	return regions, nil
}

// faultEvent is one userfaultfd message.
type faultEvent struct {
	kind    uint8
	address uint64
	start   uint64
	end     uint64
}

// userfaults is the userfaultfd Firecracker registered guest memory with, as the page server uses it.
type userfaults interface {
	next() (faultEvent, error)
	copyPage(address uint64, page []byte) error
	zeroRange(address, length uint64) error
	close() error
}

type addressRange struct {
	start uint64
	end   uint64
}

// pageServer answers one VM's page faults from its snapshot's memory while the cache is still pulling it, so the
// clone starts before the memory file is local (DESIGN.md 6.1.2, UFFD lazy loading while the file is remote).
type pageServer struct {
	source   vmm.PageSource
	listener net.Listener
	done     chan struct{}
	mu       sync.Mutex
	faults   userfaults
	regions  []uffdRegion
	removed  []addressRange
	stopped  bool
	err      error
}

func listenForPages(socketPath string, source vmm.PageSource) (*pageServer, error) {
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("listen for firecracker's userfaultfd: %w", err)
	}
	return newPageServer(source, listener), nil
}

func newPageServer(source vmm.PageSource, listener net.Listener) *pageServer {
	return &pageServer{source: source, listener: listener, done: make(chan struct{})}
}

// run takes the userfaultfd Firecracker sends while it loads the snapshot, then serves faults until stopped.
func (server *pageServer) run() {
	defer close(server.done)
	faults, regions, err := acceptUserfaults(server.listener)
	server.listener.Close()
	if err != nil {
		server.fail(err)
		return
	}
	server.mu.Lock()
	if server.stopped {
		server.mu.Unlock()
		faults.close()
		return
	}
	server.faults, server.regions = faults, regions
	server.mu.Unlock()
	server.fail(server.serve(faults))
}

// fail keeps the first error the server hit on its own; what stopping it causes is not a failure.
func (server *pageServer) fail(err error) {
	server.mu.Lock()
	defer server.mu.Unlock()
	if !server.stopped && server.err == nil {
		server.err = err
	}
}

// stop ends the server and reports a failure it had before.
func (server *pageServer) stop() error {
	server.mu.Lock()
	server.stopped = true
	faults := server.faults
	server.mu.Unlock()
	server.listener.Close()
	if faults != nil {
		faults.close()
	}
	<-server.done
	server.mu.Lock()
	defer server.mu.Unlock()
	return server.err
}

func (server *pageServer) serve(faults userfaults) error {
	for {
		event, err := faults.next()
		if err != nil {
			return err
		}
		switch event.kind {
		case uffdEventPagefault:
			if err := server.fault(faults, event.address); err != nil {
				return err
			}
		case uffdEventRemove:
			server.removed = append(server.removed, addressRange{start: event.start, end: event.end})
		}
	}
}

// fault copies the page holding address from the memory file; pages the guest gave back read as zeros.
func (server *pageServer) fault(faults userfaults, address uint64) error {
	for _, region := range server.regions {
		if !region.contains(address) {
			continue
		}
		pageSize := region.pageSize()
		pageStart := address &^ (pageSize - 1)
		if server.wasRemoved(pageStart) {
			return faults.zeroRange(pageStart, pageSize)
		}
		page := make([]byte, pageSize)
		offset := int64(region.Offset + pageStart - region.BaseHostVirtAddr)
		read, err := server.source.ReadAt(page, offset)
		if err != nil && !errors.Is(err, io.EOF) {
			return fmt.Errorf("read guest memory at %d: %w", offset, err)
		}
		clear(page[read:])
		return faults.copyPage(pageStart, page)
	}
	return fmt.Errorf("page fault at %#x is outside guest memory", address)
}

func (server *pageServer) wasRemoved(address uint64) bool {
	for _, removed := range server.removed {
		if address >= removed.start && address < removed.end {
			return true
		}
	}
	return false
}
