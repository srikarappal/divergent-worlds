package websocket

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// echoServer upgrades every request and echoes binary messages until the client closes.
type echoServer struct {
	errors chan error
}

func (server echoServer) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	conn, err := Upgrade(writer, request, 1024)
	if err != nil {
		return
	}
	defer conn.Close()
	for {
		message, err := conn.ReadMessage()
		if err != nil {
			server.errors <- err
			return
		}
		if err := conn.WriteBinary(message); err != nil {
			server.errors <- err
			return
		}
	}
}

type testClient struct {
	connection net.Conn
	reader     *bufio.Reader
}

func dial(t *testing.T, address, extraHeaders string) (*testClient, string) {
	t.Helper()
	connection, err := net.Dial("tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	request := "GET /relay HTTP/1.1\r\nHost: " + address + "\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n" +
		"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n" + extraHeaders + "\r\n"
	if _, err := connection.Write([]byte(request)); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(connection)
	var response strings.Builder
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if line == "\r\n" {
			break
		}
		response.WriteString(line)
	}
	return &testClient{connection: connection, reader: reader}, response.String()
}

func (client *testClient) send(t *testing.T, final bool, opcode byte, payload []byte) {
	t.Helper()
	first := opcode
	if final {
		first |= 0x80
	}
	header := []byte{first}
	if len(payload) < 126 {
		header = append(header, 0x80|byte(len(payload)))
	} else {
		header = append(header, 0x80|126, byte(len(payload)>>8), byte(len(payload)))
	}
	mask := []byte{0x12, 0x34, 0x56, 0x78}
	masked := make([]byte, len(payload))
	for index, value := range payload {
		masked[index] = value ^ mask[index%4]
	}
	frame := append(append(header, mask...), masked...)
	if _, err := client.connection.Write(frame); err != nil {
		t.Fatal(err)
	}
}

func (client *testClient) receive(t *testing.T) (byte, []byte) {
	t.Helper()
	var header [2]byte
	if _, err := io.ReadFull(client.reader, header[:]); err != nil {
		t.Fatal(err)
	}
	if header[1]&0x80 != 0 {
		t.Fatal("server frames must not be masked")
	}
	length := int(header[1] & 0x7F)
	if length == 126 {
		var extended [2]byte
		io.ReadFull(client.reader, extended[:])
		length = int(binary.BigEndian.Uint16(extended[:]))
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(client.reader, payload); err != nil {
		t.Fatal(err)
	}
	return header[0] & 0x0F, payload
}

func startEchoServer(t *testing.T) (*httptest.Server, chan error) {
	t.Helper()
	failures := make(chan error, 4)
	server := httptest.NewServer(echoServer{errors: failures})
	t.Cleanup(server.Close)
	return server, failures
}

func TestTheHandshakeAcceptsTheKeyAndPicksBinary(t *testing.T) {
	if AcceptKey("dGhlIHNhbXBsZSBub25jZQ==") != "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" {
		t.Fatal("accept key differs from RFC 6455's worked example")
	}
	server, _ := startEchoServer(t)
	client, response := dial(t, server.Listener.Addr().String(), "Sec-WebSocket-Protocol: base64, binary\r\n")
	defer client.connection.Close()
	for _, expected := range []string{"101 Switching Protocols", "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
		"Sec-WebSocket-Protocol: binary"} {
		if !strings.Contains(response, expected) {
			t.Fatalf("response lacks %q:\n%s", expected, response)
		}
	}
}

func TestMessagesFragmentsPingsAndClose(t *testing.T) {
	server, failures := startEchoServer(t)
	client, _ := dial(t, server.Listener.Addr().String(), "")
	defer client.connection.Close()
	client.send(t, true, opBinary, []byte("RFB 003.008\n"))
	if opcode, payload := client.receive(t); opcode != opBinary || string(payload) != "RFB 003.008\n" {
		t.Fatalf("echo: %d %q", opcode, payload)
	}
	client.send(t, false, opBinary, []byte("pointer "))
	client.send(t, true, opPing, []byte("are you there"))
	if opcode, payload := client.receive(t); opcode != opPong || string(payload) != "are you there" {
		t.Fatalf("pong: %d %q", opcode, payload)
	}
	client.send(t, true, opContinuation, bytes.Repeat([]byte("x"), 300))
	if _, payload := client.receive(t); len(payload) != len("pointer ")+300 {
		t.Fatalf("fragments join: %d bytes", len(payload))
	}
	client.send(t, true, opClose, []byte{0x03, 0xE8})
	if opcode, _ := client.receive(t); opcode != opClose {
		t.Fatalf("close answer: %d", opcode)
	}
	if err := <-failures; !errors.Is(err, ErrClosed) {
		t.Fatalf("server saw: %v", err)
	}
}

func TestOversizedAndUnmaskedFramesAreRefused(t *testing.T) {
	server, failures := startEchoServer(t)
	client, _ := dial(t, server.Listener.Addr().String(), "")
	client.send(t, true, opBinary, bytes.Repeat([]byte("x"), 2000))
	if err := <-failures; !errors.Is(err, ErrTooLarge) {
		t.Fatalf("oversized: %v", err)
	}
	client.connection.Close()
	unmasked, _ := dial(t, server.Listener.Addr().String(), "")
	defer unmasked.connection.Close()
	unmasked.connection.Write([]byte{0x82, 0x01, 'x'})
	if err := <-failures; err == nil || !strings.Contains(err.Error(), "masked") {
		t.Fatalf("unmasked: %v", err)
	}
}

func TestPlainRequestsAreNotUpgraded(t *testing.T) {
	server, _ := startEchoServer(t)
	response, err := http.Get(server.URL + "/relay")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusBadRequest {
		t.Fatalf("status: %d", response.StatusCode)
	}
}
