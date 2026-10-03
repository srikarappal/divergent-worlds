package takeover

import (
	"encoding/binary"
	"slices"
	"strings"
	"testing"
	"time"
)

// steppingClock moves 25 ms every time the recorder reads it.
type steppingClock struct {
	current time.Time
}

func (clock *steppingClock) now() time.Time {
	clock.current = clock.current.Add(25 * time.Millisecond)
	return clock.current
}

func pointer(buttons byte, x, y uint16) []byte {
	message := []byte{5, buttons, 0, 0, 0, 0}
	binary.BigEndian.PutUint16(message[2:4], x)
	binary.BigEndian.PutUint16(message[4:6], y)
	return message
}

func key(down bool, keysym uint32) []byte {
	message := []byte{4, 0, 0, 0, 0, 0, 0, 0}
	if down {
		message[1] = 1
	}
	binary.BigEndian.PutUint32(message[4:8], keysym)
	return message
}

func session(handshake ...[]byte) []byte {
	stream := []byte("RFB 003.008\n")
	for _, part := range handshake {
		stream = append(stream, part...)
	}
	setEncodings := []byte{2, 0, 0, 2, 0, 0, 0, 7, 0xff, 0xff, 0xff, 0x21}
	update := []byte{3, 1, 0, 0, 0, 0, 5, 0, 3, 32}
	cutText := []byte{6, 0, 0, 0, 0, 0, 0, 5, 'h', 'e', 'l', 'l', 'o'}
	fence := []byte{248, 0, 0, 0, 0, 0, 0, 0, 2, 'a', 'b'}
	stream = append(stream, setEncodings...)
	stream = append(stream, update...)
	stream = append(stream, pointer(0, 100, 200)...)
	stream = append(stream, pointer(1, 100, 200)...)
	stream = append(stream, cutText...)
	stream = append(stream, fence...)
	stream = append(stream, pointer(0, 101, 200)...)
	stream = append(stream, key(true, 0xffe3)...)
	return append(stream, key(false, 0xffe3)...)
}

func record(stream []byte, chunk int) ([]Event, string) {
	clock := &steppingClock{current: time.Date(2026, 10, 3, 12, 0, 0, 0, time.UTC)}
	recorder := NewRecorder(clock.current, clock.now)
	for start := 0; start < len(stream); start += chunk {
		recorder.Feed(stream[start:min(start+chunk, len(stream))])
	}
	return recorder.Events()
}

func TestTheRecorderFollowsTheHandshakeAndKeepsInput(t *testing.T) {
	stream := session([]byte{1}, []byte{1})
	whole, lost := record(stream, len(stream))
	if lost != "" || len(whole) != 5 {
		t.Fatalf("events %+v lost %q", whole, lost)
	}
	press := whole[1]
	if press.Kind != "pointer" || press.Buttons != 1 || press.X != 100 || press.Y != 200 || press.AtMillis != 50 {
		t.Fatalf("press: %+v", press)
	}
	if control := whole[3]; control.Kind != "key" || control.Keysym != 0xffe3 || !control.Down {
		t.Fatalf("key: %+v", control)
	}
	for _, chunk := range []int{1, 3, 7} {
		chunked, chunkLost := record(stream, chunk)
		if chunkLost != "" || len(chunked) != len(whole) || chunked[4].Keysym != 0xffe3 || chunked[4].Down {
			t.Fatalf("chunks of %d: %+v %q", chunk, chunked, chunkLost)
		}
	}
}

func TestVncAuthAndOldClientsAreFollowed(t *testing.T) {
	withAuth, lost := record(session([]byte{2}, make([]byte, 16), []byte{1}), 5)
	if lost != "" || len(withAuth) != 5 {
		t.Fatalf("vnc auth: %d %q", len(withAuth), lost)
	}
	old := append([]byte("RFB 003.003\n"), 1)
	old = append(old, pointer(4, 10, 10)...)
	events, lost := record(old, 2)
	if lost != "" || len(events) != 1 || events[0].Buttons != 4 {
		t.Fatalf("3.3 client: %+v %q", events, lost)
	}
}

func TestQemuExtendedKeysCount(t *testing.T) {
	stream := append([]byte("RFB 003.008\n"), 1, 1)
	extended := []byte{255, 0, 0, 1, 0, 0, 0, 0x61, 0, 0, 0, 30}
	events, lost := record(append(stream, extended...), 4)
	if lost != "" || len(events) != 1 || events[0].Keysym != 0x61 || !events[0].Down {
		t.Fatalf("qemu key: %+v %q", events, lost)
	}
}

func TestUnknownMessagesStopRecordingButKeepWhatCameBefore(t *testing.T) {
	stream := append([]byte("RFB 003.008\n"), 1, 1)
	stream = append(stream, pointer(1, 5, 5)...)
	stream = append(stream, 77, 1, 2, 3)
	stream = append(stream, pointer(0, 6, 6)...)
	events, lost := record(stream, 64)
	if len(events) != 1 || !strings.Contains(lost, "type 77") {
		t.Fatalf("events %+v lost %q", events, lost)
	}
	_, unsupported := record(append([]byte("RFB 003.008\n"), 19), 64)
	if !strings.Contains(unsupported, "security type 19") {
		t.Fatalf("security: %q", unsupported)
	}
	_, garbage := record([]byte("GET / HTTP/1.1\r\n"), 64)
	if !strings.Contains(garbage, "RFB version") {
		t.Fatalf("garbage: %q", garbage)
	}
	kinds := []string{}
	for _, event := range events {
		kinds = append(kinds, event.Kind)
	}
	if !slices.Equal(kinds, []string{"pointer"}) {
		t.Fatalf("kinds: %v", kinds)
	}
}
