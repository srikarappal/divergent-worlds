// Package websocket is the server side of RFC 6455 that takeover relays need (DESIGN.md section 6.14).
//
// It upgrades one HTTP request and reads client messages with their masks
// removed and fragments joined. It answers pings and close frames and writes
// binary messages. noVNC speaks RFB over binary messages and asks for the binary
// subprotocol, which the upgrade selects when offered.
package websocket

import (
	"bufio"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
)

const (
	acceptGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

	opContinuation = 0x0
	opText         = 0x1
	opBinary       = 0x2
	opClose        = 0x8
	opPing         = 0x9
	opPong         = 0xA
)

var (
	// ErrClosed means the peer closed the connection with a close frame.
	ErrClosed = errors.New("websocket closed")
	// ErrTooLarge means a message exceeded the size limit.
	ErrTooLarge = errors.New("websocket message too large")
)

// Conn is one upgraded connection; reads come from one goroutine, writes from any.
type Conn struct {
	connection      net.Conn
	reader          *bufio.Reader
	maxMessageBytes int
	writeMu         sync.Mutex
	closed          atomic.Bool
}

// AcceptKey is the Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key.
func AcceptKey(key string) string {
	digest := sha1.Sum([]byte(key + acceptGUID))
	return base64.StdEncoding.EncodeToString(digest[:])
}

func headerHasToken(header http.Header, name, token string) bool {
	for _, value := range header.Values(name) {
		for _, part := range strings.Split(value, ",") {
			if strings.EqualFold(strings.TrimSpace(part), token) {
				return true
			}
		}
	}
	return false
}

func chosenProtocol(header http.Header) string {
	var offered []string
	for _, value := range header.Values("Sec-WebSocket-Protocol") {
		for _, part := range strings.Split(value, ",") {
			if trimmed := strings.TrimSpace(part); trimmed != "" {
				offered = append(offered, trimmed)
			}
		}
	}
	for _, protocol := range offered {
		if protocol == "binary" {
			return protocol
		}
	}
	if len(offered) > 0 {
		return offered[0]
	}
	return ""
}

// Upgrade answers the handshake and takes over the connection; on failure it has already written the HTTP error.
func Upgrade(writer http.ResponseWriter, request *http.Request, maxMessageBytes int) (*Conn, error) {
	key := request.Header.Get("Sec-WebSocket-Key")
	if request.Method != http.MethodGet || !headerHasToken(request.Header, "Connection", "upgrade") ||
		!headerHasToken(request.Header, "Upgrade", "websocket") || key == "" ||
		request.Header.Get("Sec-WebSocket-Version") != "13" {
		http.Error(writer, "a websocket upgrade is required", http.StatusBadRequest)
		return nil, errors.New("not a websocket upgrade")
	}
	connection, buffered, err := http.NewResponseController(writer).Hijack()
	if err != nil {
		http.Error(writer, "the connection cannot be upgraded", http.StatusInternalServerError)
		return nil, err
	}
	response := "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
		"Sec-WebSocket-Accept: " + AcceptKey(key) + "\r\n"
	if protocol := chosenProtocol(request.Header); protocol != "" {
		response += "Sec-WebSocket-Protocol: " + protocol + "\r\n"
	}
	if _, err := buffered.WriteString(response + "\r\n"); err != nil {
		connection.Close()
		return nil, err
	}
	if err := buffered.Flush(); err != nil {
		connection.Close()
		return nil, err
	}
	return &Conn{connection: connection, reader: buffered.Reader, maxMessageBytes: maxMessageBytes}, nil
}

type frame struct {
	final   bool
	opcode  byte
	payload []byte
}

func (conn *Conn) readFrame() (frame, error) {
	var header [2]byte
	if _, err := io.ReadFull(conn.reader, header[:]); err != nil {
		return frame{}, err
	}
	final, opcode := header[0]&0x80 != 0, header[0]&0x0F
	if header[1]&0x80 == 0 {
		return frame{}, errors.New("client frames must be masked")
	}
	length := uint64(header[1] & 0x7F)
	switch length {
	case 126:
		var extended [2]byte
		if _, err := io.ReadFull(conn.reader, extended[:]); err != nil {
			return frame{}, err
		}
		length = uint64(binary.BigEndian.Uint16(extended[:]))
	case 127:
		var extended [8]byte
		if _, err := io.ReadFull(conn.reader, extended[:]); err != nil {
			return frame{}, err
		}
		length = binary.BigEndian.Uint64(extended[:])
	}
	if length > uint64(conn.maxMessageBytes) {
		return frame{}, ErrTooLarge
	}
	var mask [4]byte
	if _, err := io.ReadFull(conn.reader, mask[:]); err != nil {
		return frame{}, err
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(conn.reader, payload); err != nil {
		return frame{}, err
	}
	for index := range payload {
		payload[index] ^= mask[index%4]
	}
	return frame{final: final, opcode: opcode, payload: payload}, nil
}

// ReadMessage returns the next text or binary message, answering control frames on the way.
func (conn *Conn) ReadMessage() ([]byte, error) {
	var message []byte
	started := false
	for {
		next, err := conn.readFrame()
		if err != nil {
			return nil, err
		}
		switch next.opcode {
		case opPing:
			if err := conn.writeFrame(opPong, next.payload); err != nil {
				return nil, err
			}
			continue
		case opPong:
			continue
		case opClose:
			conn.writeFrame(opClose, next.payload)
			return nil, ErrClosed
		case opText, opBinary:
			if started {
				return nil, errors.New("a new message started before the last one finished")
			}
			started = true
		case opContinuation:
			if !started {
				return nil, errors.New("a continuation frame without a message")
			}
		default:
			return nil, fmt.Errorf("unknown opcode %d", next.opcode)
		}
		if len(message)+len(next.payload) > conn.maxMessageBytes {
			return nil, ErrTooLarge
		}
		message = append(message, next.payload...)
		if next.final {
			return message, nil
		}
	}
}

func (conn *Conn) writeFrame(opcode byte, payload []byte) error {
	header := []byte{0x80 | opcode}
	switch {
	case len(payload) < 126:
		header = append(header, byte(len(payload)))
	case len(payload) <= 0xFFFF:
		header = append(header, 126, byte(len(payload)>>8), byte(len(payload)))
	default:
		header = append(header, 127)
		header = binary.BigEndian.AppendUint64(header, uint64(len(payload)))
	}
	conn.writeMu.Lock()
	defer conn.writeMu.Unlock()
	_, err := conn.connection.Write(append(header, payload...))
	return err
}

// WriteBinary sends one binary message.
func (conn *Conn) WriteBinary(payload []byte) error {
	return conn.writeFrame(opBinary, payload)
}

// Close sends a normal close frame once and closes the connection.
func (conn *Conn) Close() error {
	if !conn.closed.CompareAndSwap(false, true) {
		return nil
	}
	conn.writeFrame(opClose, []byte{0x03, 0xE8})
	return conn.connection.Close()
}
