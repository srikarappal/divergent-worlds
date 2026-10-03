package guest

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeVsock plays Firecracker's hybrid vsock socket and the guest agent behind it.
type fakeVsock struct {
	mu        sync.Mutex
	listener  net.Listener
	directory string
	refuse    bool
	requests  []map[string]any
	replies   map[string]string
}

func (fake *fakeVsock) socketPath(string) string {
	return filepath.Join(fake.directory, "v.sock")
}

func (fake *fakeVsock) serve() {
	for {
		connection, err := fake.listener.Accept()
		if err != nil {
			return
		}
		fake.answer(connection)
	}
}

func (fake *fakeVsock) answer(connection net.Conn) {
	defer connection.Close()
	reader := bufio.NewReader(connection)
	handshake, err := reader.ReadString('\n')
	if err == nil && handshake == "CONNECT 5901\n" {
		connection.Write([]byte("OK 1073741825\nRFB 003.008\n"))
		echo(reader, connection)
		return
	}
	if err != nil || handshake != "CONNECT 5005\n" {
		return
	}
	fake.mu.Lock()
	refuse := fake.refuse
	fake.mu.Unlock()
	if refuse {
		connection.Write([]byte("FAILURE\n"))
		return
	}
	connection.Write([]byte("OK 1073741824\n"))
	line, err := reader.ReadBytes('\n')
	if err != nil {
		return
	}
	var request map[string]any
	json.Unmarshal(line, &request)
	fake.mu.Lock()
	fake.requests = append(fake.requests, request)
	reply := fake.replies[request["method"].(string)]
	fake.mu.Unlock()
	connection.Write([]byte(strings.ReplaceAll(reply, "ID", "1") + "\n"))
}

func (fake *fakeVsock) close() {
	fake.listener.Close()
	os.RemoveAll(fake.directory)
}

func startFakeVsock(t *testing.T, replies map[string]string) *fakeVsock {
	t.Helper()
	directory, err := os.MkdirTemp("/tmp", "vsock")
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("unix", filepath.Join(directory, "v.sock"))
	if err != nil {
		os.RemoveAll(directory)
		t.Fatal(err)
	}
	fake := &fakeVsock{listener: listener, directory: directory, replies: replies}
	t.Cleanup(fake.close)
	go fake.serve()
	return fake
}

func newTestChannel(t *testing.T, fake *fakeVsock) (*Channel, string) {
	t.Helper()
	frameRoot := t.TempDir()
	dialer := VsockDialer{SocketPath: fake.socketPath, Port: 5005}
	vncDialer := VsockDialer{SocketPath: fake.socketPath, Port: 5901}
	return NewChannel(dialer, vncDialer, NewFilesystemFrames(frameRoot), 2*time.Second), frameRoot
}

func TestPrepareSendsTheWorldClockAndEntropy(t *testing.T) {
	fake := startFakeVsock(t, map[string]string{"prepare": `{"id": ID, "result": {}}`})
	channel, _ := newTestChannel(t, fake)
	worldTime := time.Date(2026, 3, 14, 9, 0, 5, 0, time.UTC)
	if err := channel.Prepare(context.Background(), "vm_1", worldTime, []byte{1, 2, 3}); err != nil {
		t.Fatalf("prepare: %v", err)
	}
	if err := channel.Prepare(context.Background(), "vm_1", worldTime, nil); err != nil {
		t.Fatalf("prepare without entropy: %v", err)
	}
	params := fake.requests[0]["params"].(map[string]any)
	if params["now"] != "2026-03-14T09:00:05Z" || params["entropy"] != base64.StdEncoding.EncodeToString([]byte{1, 2, 3}) {
		t.Fatalf("prepare params: %v", params)
	}
	if _, sent := fake.requests[1]["params"].(map[string]any)["entropy"]; sent {
		t.Fatal("entropy was sent again")
	}
}

func TestObserveStoresTheScreenshotAsABlob(t *testing.T) {
	frame := []byte("RIFF....WEBPVP8 frame")
	reply := `{"id": ID, "result": {"step_index": 0, "screenshot_data": "` + base64.StdEncoding.EncodeToString(frame) +
		`", "screenshot_size": [1280, 800], "accessibility": []}}`
	fake := startFakeVsock(t, map[string]string{"observe": reply})
	channel, frameRoot := newTestChannel(t, fake)
	raw, err := channel.Observe(context.Background(), "vm_1", []string{"screenshot", "accessibility_tree"})
	if err != nil {
		t.Fatalf("observe: %v", err)
	}
	var observation map[string]any
	json.Unmarshal(raw, &observation)
	screenshot := observation["screenshot"].(map[string]any)
	digest := screenshot["sha256"].(string)
	if screenshot["uri"] != "blob:sha256:"+digest || screenshot["width"].(float64) != 1280 ||
		screenshot["image_format"] != "webp" {
		t.Fatalf("screenshot ref: %v", screenshot)
	}
	if _, kept := observation["screenshot_data"]; kept {
		t.Fatal("the raw frame stayed in the observation")
	}
	stored, err := os.ReadFile(filepath.Join(frameRoot, "blobs", digest[:2], digest))
	if err != nil || string(stored) != string(frame) {
		t.Fatalf("stored frame: %q %v", stored, err)
	}
	kinds := fake.requests[0]["params"].(map[string]any)["kinds"].([]any)
	if len(kinds) != 2 || kinds[1] != "accessibility_tree" {
		t.Fatalf("observation kinds: %v", kinds)
	}
}

func TestGuestErrorsAndRefusedHandshakes(t *testing.T) {
	fake := startFakeVsock(t, map[string]string{
		"act":         `{"id": ID, "error": {"code": "unsupported_action", "message": "no native API"}}`,
		"screen_hash": `{"id": ID, "result": "abc123"}`,
		"extract":     `{"id": ID, "result": {"files": {}}}`,
	})
	channel, _ := newTestChannel(t, fake)
	err := channel.Act(context.Background(), "vm_1", json.RawMessage(`{"name": "native_call"}`))
	var failure *Error
	if !errors.As(err, &failure) || failure.Code != "unsupported_action" {
		t.Fatalf("act: %v", err)
	}
	if hash, err := channel.ScreenHash(context.Background(), "vm_1"); err != nil || hash != "abc123" {
		t.Fatalf("screen hash: %q %v", hash, err)
	}
	if state, err := channel.Extract(context.Background(), "vm_1", []string{"files"}); err != nil ||
		string(state) != `{"files": {}}` {
		t.Fatalf("extract: %s %v", state, err)
	}
	fake.mu.Lock()
	fake.refuse = true
	fake.mu.Unlock()
	if err := channel.Inject(context.Background(), "vm_1", json.RawMessage(`{}`)); err == nil ||
		!strings.Contains(err.Error(), "refused") {
		t.Fatalf("refused handshake: %v", err)
	}
}

func TestFramesAreStoredOnce(t *testing.T) {
	root := t.TempDir()
	frames := NewFilesystemFrames(root)
	first, err := frames.Put([]byte("same screen"))
	if err != nil {
		t.Fatal(err)
	}
	second, _ := frames.Put([]byte("same screen"))
	entries, _ := os.ReadDir(filepath.Join(root, "blobs", first[:2]))
	if first != second || len(entries) != 1 {
		t.Fatalf("digests %s %s, files %d", first, second, len(entries))
	}
}

// fakeContainerAgent plays a development container's agent: TCP, then a token before any request.
type fakeContainerAgent struct {
	mu       sync.Mutex
	listener net.Listener
	token    string
}

func (fake *fakeContainerAgent) close() {
	fake.listener.Close()
}

func (fake *fakeContainerAgent) setToken(token string) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	fake.token = token
}

func (fake *fakeContainerAgent) GuestEndpoint(vmID string) (string, string, error) {
	if vmID != "env_1" {
		return "", "", errors.New("no such container")
	}
	return fake.listener.Addr().String(), "s3cret", nil
}

func (fake *fakeContainerAgent) serve() {
	for {
		connection, err := fake.listener.Accept()
		if err != nil {
			return
		}
		fake.answer(connection)
	}
}

func (fake *fakeContainerAgent) answer(connection net.Conn) {
	defer connection.Close()
	reader := bufio.NewReader(connection)
	greeting, err := reader.ReadString('\n')
	fake.mu.Lock()
	token := fake.token
	fake.mu.Unlock()
	if err == nil && greeting == "VNC "+token+"\n" {
		connection.Write([]byte("OK\nRFB 003.008\n"))
		echo(reader, connection)
		return
	}
	if err != nil || greeting != "AUTH "+token+"\n" {
		return
	}
	connection.Write([]byte("OK\n"))
	if _, err := reader.ReadBytes('\n'); err == nil {
		connection.Write([]byte(`{"id": 1, "result": "hash"}` + "\n"))
	}
}

func TestTokenDialerProvesItselfToContainerAgents(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	fake := &fakeContainerAgent{listener: listener, token: "s3cret"}
	t.Cleanup(fake.close)
	go fake.serve()
	channel := NewChannel(TokenDialer{Endpoints: fake}, TokenDialer{Endpoints: fake, Greeting: "VNC"},
		NewFilesystemFrames(t.TempDir()), 2*time.Second)
	if hash, err := channel.ScreenHash(context.Background(), "env_1"); err != nil || hash != "hash" {
		t.Fatalf("screen hash over tcp: %q %v", hash, err)
	}
	fake.setToken("rotated")
	if _, err := channel.ScreenHash(context.Background(), "env_1"); err == nil {
		t.Fatal("a wrong token must not reach the agent")
	}
	if _, err := channel.ScreenHash(context.Background(), "env_2"); err == nil ||
		!strings.Contains(err.Error(), "no such container") {
		t.Fatalf("unknown container: %v", err)
	}
}

// echo plays a VNC server that repeats whatever the client sends until it hangs up.
func echo(reader *bufio.Reader, connection net.Conn) {
	buffer := make([]byte, 256)
	for {
		count, err := reader.Read(buffer)
		if err != nil {
			return
		}
		connection.Write(buffer[:count])
	}
}

func readVNC(t *testing.T, connection net.Conn, reader *bufio.Reader) {
	t.Helper()
	defer connection.Close()
	banner := make([]byte, 12)
	if _, err := io.ReadFull(reader, banner); err != nil || string(banner) != "RFB 003.008\n" {
		t.Fatalf("banner: %q %v", banner, err)
	}
	connection.Write([]byte("pointer"))
	echoed := make([]byte, 7)
	if _, err := io.ReadFull(reader, echoed); err != nil || string(echoed) != "pointer" {
		t.Fatalf("echo: %q %v", echoed, err)
	}
}

func TestVNCRelaysOpenOverVsockAndTokens(t *testing.T) {
	fake := startFakeVsock(t, map[string]string{})
	channel, _ := newTestChannel(t, fake)
	connection, reader, err := channel.DialVNC(context.Background(), "vm_1")
	if err != nil {
		t.Fatal(err)
	}
	readVNC(t, connection, reader)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	container := &fakeContainerAgent{listener: listener, token: "s3cret"}
	t.Cleanup(container.close)
	go container.serve()
	tokens := NewChannel(TokenDialer{Endpoints: container}, TokenDialer{Endpoints: container, Greeting: "VNC"},
		NewFilesystemFrames(t.TempDir()), 2*time.Second)
	connection, reader, err = tokens.DialVNC(context.Background(), "env_1")
	if err != nil {
		t.Fatal(err)
	}
	readVNC(t, connection, reader)
}
