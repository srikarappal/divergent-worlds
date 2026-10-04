//go:build linux

package firecracker

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"runtime"
	"syscall"
	"unsafe"
)

// ioctl requests and the message size from linux/userfaultfd.h.
const (
	uffdioCopy      = 0xc028aa03
	uffdioZeropage  = 0xc020aa04
	uffdMessageSize = 32
	copyAttempts    = 8
)

type uffdioCopyArgument struct {
	dst    uint64
	src    uint64
	length uint64
	mode   uint64
	copied int64
}

type uffdioZeropageArgument struct {
	start  uint64
	length uint64
	mode   uint64
	zeroed int64
}

// uffdFile is the userfaultfd Firecracker sent over the page socket, read through the runtime poller.
type uffdFile struct {
	file *os.File
	fd   uintptr
}

// acceptUserfaults waits for Firecracker on the page socket and takes the userfaultfd and region list it sends
// in one message.
func acceptUserfaults(listener net.Listener) (userfaults, []uffdRegion, error) {
	accepted, err := listener.Accept()
	if err != nil {
		return nil, nil, err
	}
	defer accepted.Close()
	connection, ok := accepted.(*net.UnixConn)
	if !ok {
		return nil, nil, errors.New("the page socket is not a unix socket")
	}
	body := make([]byte, 64<<10)
	control := make([]byte, syscall.CmsgSpace(4))
	bodyLength, controlLength, _, _, err := connection.ReadMsgUnix(body, control)
	if err != nil {
		return nil, nil, err
	}
	messages, err := syscall.ParseSocketControlMessage(control[:controlLength])
	if err != nil || len(messages) != 1 {
		return nil, nil, fmt.Errorf("firecracker sent no userfaultfd: %v", err)
	}
	fds, err := syscall.ParseUnixRights(&messages[0])
	if err != nil || len(fds) != 1 {
		return nil, nil, fmt.Errorf("firecracker sent no userfaultfd: %v", err)
	}
	regions, err := parseRegions(body[:bodyLength])
	if err != nil {
		syscall.Close(fds[0])
		return nil, nil, err
	}
	if err := syscall.SetNonblock(fds[0], true); err != nil {
		syscall.Close(fds[0])
		return nil, nil, err
	}
	return &uffdFile{file: os.NewFile(uintptr(fds[0]), "userfaultfd"), fd: uintptr(fds[0])}, regions, nil
}

func (faults *uffdFile) next() (faultEvent, error) {
	message := make([]byte, uffdMessageSize)
	if _, err := io.ReadFull(faults.file, message); err != nil {
		return faultEvent{}, err
	}
	event := faultEvent{kind: message[0]}
	switch event.kind {
	case uffdEventPagefault:
		event.address = binary.LittleEndian.Uint64(message[16:24])
	case uffdEventRemove:
		event.start = binary.LittleEndian.Uint64(message[8:16])
		event.end = binary.LittleEndian.Uint64(message[16:24])
	}
	return event, nil
}

// copyPage maps the page into the guest and wakes the faulting vCPU; a page another fault mapped first is fine.
func (faults *uffdFile) copyPage(address uint64, page []byte) error {
	argument := uffdioCopyArgument{dst: address, src: uint64(uintptr(unsafe.Pointer(&page[0]))),
		length: uint64(len(page))}
	err := faults.ioctl(uffdioCopy, unsafe.Pointer(&argument))
	runtime.KeepAlive(page)
	return err
}

func (faults *uffdFile) zeroRange(address, length uint64) error {
	argument := uffdioZeropageArgument{start: address, length: length}
	return faults.ioctl(uffdioZeropage, unsafe.Pointer(&argument))
}

// ioctl retries while the guest's memory layout is changing underneath, as the kernel asks.
func (faults *uffdFile) ioctl(request uintptr, argument unsafe.Pointer) error {
	for range copyAttempts {
		_, _, errno := syscall.Syscall(syscall.SYS_IOCTL, faults.fd, request, uintptr(argument))
		switch errno {
		case 0, syscall.EEXIST:
			return nil
		case syscall.EAGAIN:
			continue
		default:
			return errno
		}
	}
	return fmt.Errorf("userfaultfd ioctl %#x kept returning EAGAIN", request)
}

func (faults *uffdFile) close() error {
	return faults.file.Close()
}
