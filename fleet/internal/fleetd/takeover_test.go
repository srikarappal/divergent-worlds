package fleetd

import (
	"bufio"
	"encoding/binary"
	"io"
	"net"
	"net/http"
	"slices"
	"strings"
	"testing"
	"time"
)

// relayClient is the operator's noVNC: a websocket client sending masked binary frames.
type relayClient struct {
	connection net.Conn
	reader     *bufio.Reader
}

func dialRelay(t *testing.T, fleet *testFleet, relayPath string) (*relayClient, string) {
	t.Helper()
	address := fleet.server.Listener.Addr().String()
	connection, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	request := "GET " + relayPath + " HTTP/1.1\r\nHost: " + address + "\r\nUpgrade: websocket\r\n" +
		"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n" +
		"Sec-WebSocket-Protocol: binary\r\n\r\n"
	connection.Write([]byte(request))
	reader := bufio.NewReader(connection)
	status, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	for {
		line, err := reader.ReadString('\n')
		if err != nil || line == "\r\n" {
			break
		}
	}
	return &relayClient{connection: connection, reader: reader}, status
}

func (client *relayClient) send(t *testing.T, payload []byte) {
	t.Helper()
	header := []byte{0x82, 0x80 | byte(len(payload)), 1, 2, 3, 4}
	masked := make([]byte, len(payload))
	for index, value := range payload {
		masked[index] = value ^ header[2+index%4]
	}
	if _, err := client.connection.Write(append(header, masked...)); err != nil {
		t.Fatal(err)
	}
}

// receive reads until want bytes of binary payload have arrived, since the relay may split or join messages.
func (client *relayClient) receive(t *testing.T, want int) []byte {
	t.Helper()
	client.connection.SetReadDeadline(time.Now().Add(5 * time.Second))
	var received []byte
	for len(received) < want {
		var header [2]byte
		if _, err := io.ReadFull(client.reader, header[:]); err != nil {
			t.Fatalf("after %q: %v", received, err)
		}
		length := int(header[1] & 0x7F)
		if length == 126 {
			var extended [2]byte
			io.ReadFull(client.reader, extended[:])
			length = int(binary.BigEndian.Uint16(extended[:]))
		}
		payload := make([]byte, length)
		io.ReadFull(client.reader, payload)
		if header[0]&0x0F == 0x2 {
			received = append(received, payload...)
		}
	}
	return received
}

func pointerEvent(buttons byte, x, y uint16) []byte {
	message := []byte{5, buttons, 0, 0, 0, 0}
	binary.BigEndian.PutUint16(message[2:4], x)
	binary.BigEndian.PutUint16(message[4:6], y)
	return message
}

func TestAnOperatorTakesOverTeleopsAndHandsBack(t *testing.T) {
	fleet := newTestFleet(t, 10, 100_000)
	_, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	leaseID := leaseIDs(t, payload)[0]
	status, started := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/takeover", nil)
	if status != http.StatusCreated || !strings.HasPrefix(started["relay_path"].(string), "/v1/takeover/") {
		t.Fatalf("start: %d %v", status, started)
	}
	if fleet.driver.Operations[len(fleet.driver.Operations)-1] != "resume env_00000001" {
		t.Fatalf("the operator's env must run: %v", fleet.driver.Operations)
	}
	status, refused := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/step",
		map[string]any{"action": map[string]any{"name": "wait", "duration_ms": 0}})
	if status != http.StatusConflict || refused["code"] != "taken_over" {
		t.Fatalf("worker step during takeover: %d %v", status, refused)
	}
	if _, beat := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/heartbeat", nil); beat["taken_over"] != true {
		t.Fatalf("heartbeat: %v", beat)
	}
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/takeover", nil); status != http.StatusConflict {
		t.Fatalf("a second takeover: %d", status)
	}

	relayPath := started["relay_path"].(string)
	client, handshake := dialRelay(t, fleet, relayPath)
	if !strings.Contains(handshake, "101") {
		t.Fatalf("relay handshake: %q", handshake)
	}
	if banner := client.receive(t, 12); string(banner) != "RFB 003.008\n" {
		t.Fatalf("banner: %q", banner)
	}
	stream := append([]byte("RFB 003.008\n"), 1, 1)
	stream = append(stream, pointerEvent(1, 40, 50)...)
	client.send(t, stream)
	client.send(t, pointerEvent(0, 40, 50))
	if echoed := client.receive(t, len(stream)+6); len(echoed) != len(stream)+6 {
		t.Fatalf("echo: %d bytes", len(echoed))
	}
	if again, status := dialRelay(t, fleet, relayPath); !strings.Contains(status, "404") {
		again.connection.Close()
		t.Fatalf("a session connects once: %q", status)
	}

	status, ended := fleet.call(t, http.MethodDelete, "/v1/leases/"+leaseID+"/takeover", nil)
	if status != http.StatusOK || ended["lost_reason"] != "" {
		t.Fatalf("end: %d %v", status, ended)
	}
	events := ended["events"].([]any)
	if len(events) != 2 || events[0].(map[string]any)["buttons"] != float64(1) ||
		events[0].(map[string]any)["x"] != float64(40) {
		t.Fatalf("recorded events: %v", events)
	}
	client.connection.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := io.ReadAll(client.reader); err != nil {
		t.Fatalf("the relay must close when the takeover ends: %v", err)
	}
	if fleet.driver.Operations[len(fleet.driver.Operations)-1] != "pause env_00000001" {
		t.Fatalf("the env pauses again: %v", fleet.driver.Operations)
	}
	if status, _ := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/step",
		map[string]any{"action": map[string]any{"name": "wait", "duration_ms": 0}}); status != http.StatusOK {
		t.Fatalf("the worker resumes after the takeover: %d", status)
	}
	if status, payload := fleet.call(t, http.MethodDelete, "/v1/leases/"+leaseID+"/takeover", nil); status !=
		http.StatusConflict || payload["code"] != "no_takeover" {
		t.Fatalf("ending twice: %d %v", status, payload)
	}
}

func TestTakeoverSessionsExpireAndDieWithTheirLease(t *testing.T) {
	fleet := newTestFleet(t, 10, 100_000)
	_, payload := fleet.call(t, http.MethodPost, "/v1/leases", leaseBody(1))
	leaseID := leaseIDs(t, payload)[0]
	_, started := fleet.call(t, http.MethodPost, "/v1/leases/"+leaseID+"/takeover", nil)
	fleet.service.now = time.Now().Add(takeoverConnectWindow + time.Second).Local
	if client, status := dialRelay(t, fleet, started["relay_path"].(string)); !strings.Contains(status, "404") {
		client.connection.Close()
		t.Fatalf("an expired session must not connect: %q", status)
	}
	fleet.service.now = time.Now
	if status, _ := fleet.call(t, http.MethodDelete, "/v1/leases/"+leaseID, nil); status != http.StatusNoContent {
		t.Fatalf("close: %d", status)
	}
	fleet.service.mu.Lock()
	remaining := len(fleet.service.takeovers)
	fleet.service.mu.Unlock()
	if remaining != 0 {
		t.Fatalf("sessions outlive their lease: %d", remaining)
	}
	if status, payload := fleet.call(t, http.MethodPost, "/v1/leases/missing/takeover", nil); status !=
		http.StatusNotFound || payload["code"] != "unknown_lease" {
		t.Fatalf("unknown lease: %d %v", status, payload)
	}
	operations := slices.Clone(fleet.driver.Operations)
	if !slices.Contains(operations, "destroy env_00000001") {
		t.Fatalf("operations: %v", operations)
	}
}
