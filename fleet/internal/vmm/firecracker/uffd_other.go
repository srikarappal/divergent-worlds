//go:build !linux

package firecracker

import (
	"errors"
	"net"
)

// acceptUserfaults waits like the Linux version, then refuses: userfaultfd exists only on Linux.
func acceptUserfaults(listener net.Listener) (userfaults, []uffdRegion, error) {
	accepted, err := listener.Accept()
	if err != nil {
		return nil, nil, err
	}
	accepted.Close()
	return nil, nil, errors.New("serving guest memory through userfaultfd needs Linux")
}
