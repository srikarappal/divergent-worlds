// Package guest reaches the Linux guest agent (DESIGN.md sections 6.1 and 6.3).
//
// Each call dials the guest, finishes a one line handshake, sends one JSON
// request line and reads one response line. VMs are reached over Firecracker's
// hybrid vsock: the host socket is asked to connect to the agent's port.
// Development containers have no vsock, so their agent listens on TCP and only
// answers a client presenting the container's token, which the policy's shell
// cannot read. Screenshots come back as bytes; the channel stores them as content
// addressed blobs and hands the node agent an observation that references them.
package guest

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net"
	"strings"
	"sync/atomic"
	"time"
)

// SocketPath names the host side vsock socket of one VM.
type SocketPath func(vmID string) string

// FrameStore keeps screenshot bytes and returns their hex sha256.
type FrameStore interface {
	Put(data []byte) (string, error)
}

// Dialer opens a connection to a VM's guest agent with its handshake done and the deadline set.
type Dialer interface {
	Dial(ctx context.Context, vmID string, deadline time.Time) (net.Conn, *bufio.Reader, error)
}

// Endpoints says where a development container's agent listens and which token it expects.
type Endpoints interface {
	GuestEndpoint(vmID string) (address, token string, err error)
}

// VsockDialer reaches the agent through Firecracker's hybrid vsock socket.
type VsockDialer struct {
	SocketPath SocketPath
	Port       uint32
}

// TokenDialer reaches the agent of a development container over TCP.
type TokenDialer struct {
	Endpoints Endpoints
}

// Error is a failure the guest agent reported, with its code.
type Error struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (failure *Error) Error() string {
	return fmt.Sprintf("guest agent %s: %s", failure.Code, failure.Message)
}

type request struct {
	ID     int64  `json:"id"`
	Method string `json:"method"`
	Params any    `json:"params"`
}

type response struct {
	ID     int64           `json:"id"`
	Result json.RawMessage `json:"result"`
	Error  *Error          `json:"error"`
}

type screenshotRef struct {
	URI         string `json:"uri"`
	Width       int    `json:"width"`
	Height      int    `json:"height"`
	ImageFormat string `json:"image_format"`
	SHA256      string `json:"sha256"`
}

// handshake sends the greeting line and accepts an answer starting with OK; on any failure it closes the connection.
func handshake(connection net.Conn, deadline time.Time, greeting, vmID string) (*bufio.Reader, error) {
	if err := connection.SetDeadline(deadline); err != nil {
		connection.Close()
		return nil, err
	}
	if _, err := fmt.Fprint(connection, greeting); err != nil {
		connection.Close()
		return nil, err
	}
	reader := bufio.NewReader(connection)
	acknowledgement, err := reader.ReadString('\n')
	if err != nil {
		connection.Close()
		return nil, fmt.Errorf("guest handshake with %s: %w", vmID, err)
	}
	if !strings.HasPrefix(acknowledgement, "OK") {
		connection.Close()
		return nil, fmt.Errorf("guest handshake with %s refused: %q", vmID, strings.TrimSpace(acknowledgement))
	}
	return reader, nil
}

// Dial connects to the VM's vsock socket and asks Firecracker for the agent's port.
func (dialer VsockDialer) Dial(ctx context.Context, vmID string, deadline time.Time) (net.Conn, *bufio.Reader, error) {
	var netDialer net.Dialer
	connection, err := netDialer.DialContext(ctx, "unix", dialer.SocketPath(vmID))
	if err != nil {
		return nil, nil, fmt.Errorf("dial guest of %s: %w", vmID, err)
	}
	reader, err := handshake(connection, deadline, fmt.Sprintf("CONNECT %d\n", dialer.Port), vmID)
	return connection, reader, err
}

// Dial connects to the container's published agent port and presents its token.
func (dialer TokenDialer) Dial(ctx context.Context, vmID string, deadline time.Time) (net.Conn, *bufio.Reader, error) {
	address, token, err := dialer.Endpoints.GuestEndpoint(vmID)
	if err != nil {
		return nil, nil, err
	}
	var netDialer net.Dialer
	connection, err := netDialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return nil, nil, fmt.Errorf("dial guest of %s: %w", vmID, err)
	}
	reader, err := handshake(connection, deadline, fmt.Sprintf("AUTH %s\n", token), vmID)
	return connection, reader, err
}

// Channel implements the node agent's guest channel.
type Channel struct {
	dialer     Dialer
	frames     FrameStore
	timeout    time.Duration
	requestIDs atomic.Int64
}

// NewChannel returns a channel; calls without a context deadline give up after timeout.
func NewChannel(dialer Dialer, frames FrameStore, timeout time.Duration) *Channel {
	return &Channel{dialer: dialer, frames: frames, timeout: timeout}
}

func (channel *Channel) call(ctx context.Context, vmID, method string, params any) (json.RawMessage, error) {
	deadline, hasDeadline := ctx.Deadline()
	if !hasDeadline {
		deadline = time.Now().Add(channel.timeout)
	}
	connection, reader, err := channel.dialer.Dial(ctx, vmID, deadline)
	if err != nil {
		return nil, err
	}
	defer connection.Close()
	encoded, err := json.Marshal(request{ID: channel.requestIDs.Add(1), Method: method, Params: params})
	if err != nil {
		return nil, err
	}
	if _, err := connection.Write(append(encoded, '\n')); err != nil {
		return nil, err
	}
	line, err := reader.ReadBytes('\n')
	if err != nil {
		return nil, fmt.Errorf("read %s reply from %s: %w", method, vmID, err)
	}
	var reply response
	if err := json.Unmarshal(line, &reply); err != nil {
		return nil, fmt.Errorf("decode %s reply from %s: %w", method, vmID, err)
	}
	if reply.Error != nil {
		return nil, reply.Error
	}
	return reply.Result, nil
}

// Prepare runs clone hygiene before a turn: the world clock always, fresh entropy once after a restore.
func (channel *Channel) Prepare(ctx context.Context, vmID string, now time.Time, entropy []byte) error {
	params := map[string]string{"now": now.UTC().Format(time.RFC3339Nano)}
	if len(entropy) > 0 {
		params["entropy"] = base64.StdEncoding.EncodeToString(entropy)
	}
	_, err := channel.call(ctx, vmID, "prepare", params)
	return err
}

// Act plays one canonical action.
func (channel *Channel) Act(ctx context.Context, vmID string, action json.RawMessage) error {
	_, err := channel.call(ctx, vmID, "act", map[string]json.RawMessage{"action": action})
	return err
}

// ScreenHash hashes the screen for settle detection.
func (channel *Channel) ScreenHash(ctx context.Context, vmID string) (string, error) {
	result, err := channel.call(ctx, vmID, "screen_hash", map[string]any{})
	if err != nil {
		return "", err
	}
	var hash string
	err = json.Unmarshal(result, &hash)
	return hash, err
}

// Observe collects the observation kinds the task asks for and stores the screenshot as a blob.
func (channel *Channel) Observe(ctx context.Context, vmID string, kinds []string) (json.RawMessage, error) {
	result, err := channel.call(ctx, vmID, "observe", map[string][]string{"kinds": kinds})
	if err != nil {
		return nil, err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(result, &fields); err != nil {
		return nil, err
	}
	if encodedFrame, found := fields["screenshot_data"]; found {
		screenshot, err := channel.storeFrame(encodedFrame, fields["screenshot_size"])
		if err != nil {
			return nil, fmt.Errorf("store screenshot of %s: %w", vmID, err)
		}
		fields["screenshot"] = screenshot
		delete(fields, "screenshot_data")
		delete(fields, "screenshot_size")
	}
	return json.Marshal(fields)
}

func (channel *Channel) storeFrame(encodedFrame, encodedSize json.RawMessage) (json.RawMessage, error) {
	var frameText string
	var size [2]int
	if err := json.Unmarshal(encodedFrame, &frameText); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(encodedSize, &size); err != nil {
		return nil, err
	}
	frame, err := base64.StdEncoding.DecodeString(frameText)
	if err != nil {
		return nil, err
	}
	digest, err := channel.frames.Put(frame)
	if err != nil {
		return nil, err
	}
	return json.Marshal(screenshotRef{URI: "blob:sha256:" + digest, Width: size[0], Height: size[1],
		ImageFormat: "webp", SHA256: digest})
}

// Inject hands world state, task setup and entitlements to the guest.
func (channel *Channel) Inject(ctx context.Context, vmID string, payload json.RawMessage) error {
	_, err := channel.call(ctx, vmID, "inject", payload)
	return err
}

// Extract reads app state for the verifiers.
func (channel *Channel) Extract(ctx context.Context, vmID string, apps []string) (json.RawMessage, error) {
	return channel.call(ctx, vmID, "extract", map[string][]string{"apps": apps})
}
