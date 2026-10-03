// Package qemu drives QEMU VMs restored by templating, mostly for the Windows pool.
package qemu

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"sync"
	"time"
)

// QMPError is an error answer from QEMU.
type QMPError struct {
	Class       string
	Description string
}

func (qmpError *QMPError) Error() string {
	return fmt.Sprintf("qmp %s: %s", qmpError.Class, qmpError.Description)
}

// QMP is the command channel the driver needs.
type QMP interface {
	Execute(ctx context.Context, command string, arguments any) (json.RawMessage, error)
	Close() error
}

// QMPClient speaks newline delimited JSON over the QMP unix socket and skips async events.
type QMPClient struct {
	mu         sync.Mutex
	connection net.Conn
	reader     *bufio.Reader
}

type qmpReply struct {
	Return json.RawMessage `json:"return"`
	Error  *struct {
		Class       string `json:"class"`
		Description string `json:"desc"`
	} `json:"error"`
	Event string `json:"event"`
}

// DialQMP connects, reads the greeting and negotiates capabilities.
func DialQMP(ctx context.Context, socketPath string) (*QMPClient, error) {
	var dialer net.Dialer
	connection, err := dialer.DialContext(ctx, "unix", socketPath)
	if err != nil {
		return nil, err
	}
	client := &QMPClient{connection: connection, reader: bufio.NewReader(connection)}
	if deadline, hasDeadline := ctx.Deadline(); hasDeadline {
		_ = connection.SetDeadline(deadline)
	}
	if _, err := client.reader.ReadBytes('\n'); err != nil {
		connection.Close()
		return nil, fmt.Errorf("read qmp greeting: %w", err)
	}
	if _, err := client.Execute(ctx, "qmp_capabilities", nil); err != nil {
		connection.Close()
		return nil, err
	}
	return client, nil
}

// Execute sends one command and waits for its return, skipping events in between.
func (client *QMPClient) Execute(ctx context.Context, command string, arguments any) (json.RawMessage, error) {
	client.mu.Lock()
	defer client.mu.Unlock()
	deadline, hasDeadline := ctx.Deadline()
	if !hasDeadline {
		deadline = time.Now().Add(2 * time.Minute)
	}
	if err := client.connection.SetDeadline(deadline); err != nil {
		return nil, err
	}
	message := map[string]any{"execute": command}
	if arguments != nil {
		message["arguments"] = arguments
	}
	encoded, err := json.Marshal(message)
	if err != nil {
		return nil, err
	}
	if _, err := client.connection.Write(append(encoded, '\n')); err != nil {
		return nil, err
	}
	for {
		line, err := client.reader.ReadBytes('\n')
		if err != nil {
			return nil, fmt.Errorf("read qmp reply to %s: %w", command, err)
		}
		var reply qmpReply
		if err := json.Unmarshal(line, &reply); err != nil {
			return nil, fmt.Errorf("decode qmp reply to %s: %w", command, err)
		}
		if reply.Event != "" {
			continue
		}
		if reply.Error != nil {
			return nil, &QMPError{Class: reply.Error.Class, Description: reply.Error.Description}
		}
		return reply.Return, nil
	}
}

// Close ends the connection.
func (client *QMPClient) Close() error {
	return client.connection.Close()
}
