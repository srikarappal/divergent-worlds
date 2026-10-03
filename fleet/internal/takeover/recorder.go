// Package takeover records what an operator does while teleoping a live env (DESIGN.md section 6.14).
//
// The relay feeds every byte the operator's noVNC client sends into a Recorder,
// which follows the RFB handshake and then the client messages, keeping pointer
// and key events with their time since the takeover began. Those events become
// the episode's demonstration. Messages the recorder cannot frame end the
// recording, not the relay: the session goes on and the demonstration is marked
// incomplete.
package takeover

import (
	"encoding/binary"
	"fmt"
	"strconv"
	"sync"
	"time"
)

// Event is one input the operator sent: pointer position and buttons, or a key going down or up.
type Event struct {
	AtMillis int64  `json:"at_ms"`
	Kind     string `json:"kind"`
	X        int    `json:"x"`
	Y        int    `json:"y"`
	Buttons  int    `json:"buttons"`
	Keysym   uint32 `json:"keysym"`
	Down     bool   `json:"down"`
}

type stage int

const (
	stageVersion stage = iota
	stageSecurity
	stageAuthResponse
	stageInit
	stageMessages
	stageLost
)

const (
	securityNone = 1
	securityVNC  = 2
)

// Recorder follows one client to server RFB stream.
type Recorder struct {
	mu         sync.Mutex
	started    time.Time
	now        func() time.Time
	stage      stage
	pending    []byte
	events     []Event
	lostReason string
}

// NewRecorder starts the clock at the moment the takeover began.
func NewRecorder(started time.Time, now func() time.Time) *Recorder {
	return &Recorder{started: started, now: now}
}

// Feed takes the next bytes the client sent, in order.
func (recorder *Recorder) Feed(data []byte) {
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	if recorder.stage == stageLost {
		return
	}
	recorder.pending = append(recorder.pending, data...)
	for recorder.stage != stageLost {
		consumed := recorder.next()
		if consumed == 0 {
			return
		}
		recorder.pending = recorder.pending[consumed:]
	}
}

// Events returns what was recorded so far and whether the stream stayed in sync.
func (recorder *Recorder) Events() ([]Event, string) {
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	return append([]Event(nil), recorder.events...), recorder.lostReason
}

func (recorder *Recorder) lose(reason string) int {
	recorder.stage = stageLost
	recorder.lostReason = reason
	return 0
}

func (recorder *Recorder) elapsed() int64 {
	return recorder.now().Sub(recorder.started).Milliseconds()
}

// next frames one handshake step or message from pending and returns how many bytes it used, or 0 to wait.
func (recorder *Recorder) next() int {
	pending := recorder.pending
	switch recorder.stage {
	case stageVersion:
		if len(pending) < 12 {
			return 0
		}
		minor, err := strconv.Atoi(string(pending[8:11]))
		if string(pending[:4]) != "RFB " || err != nil {
			return recorder.lose("the client did not open with an RFB version")
		}
		recorder.stage = stageSecurity
		if minor < 7 {
			recorder.stage = stageInit
		}
		return 12
	case stageSecurity:
		if len(pending) < 1 {
			return 0
		}
		switch pending[0] {
		case securityNone:
			recorder.stage = stageInit
		case securityVNC:
			recorder.stage = stageAuthResponse
		default:
			return recorder.lose(fmt.Sprintf("security type %d is not recorded", pending[0]))
		}
		return 1
	case stageAuthResponse:
		if len(pending) < 16 {
			return 0
		}
		recorder.stage = stageInit
		return 16
	case stageInit:
		if len(pending) < 1 {
			return 0
		}
		recorder.stage = stageMessages
		return 1
	}
	return recorder.message(pending)
}

// message frames one client message after the handshake (RFC 6143 section 7.5 and the common extensions).
func (recorder *Recorder) message(pending []byte) int {
	if len(pending) < 1 {
		return 0
	}
	switch pending[0] {
	case 0:
		return fixedLength(pending, 20)
	case 2:
		if len(pending) < 4 {
			return 0
		}
		return fixedLength(pending, 4+4*int(binary.BigEndian.Uint16(pending[2:4])))
	case 3, 150:
		return fixedLength(pending, 10)
	case 4:
		if len(pending) < 8 {
			return 0
		}
		recorder.events = append(recorder.events, Event{AtMillis: recorder.elapsed(), Kind: "key",
			Keysym: binary.BigEndian.Uint32(pending[4:8]), Down: pending[1] != 0})
		return 8
	case 5:
		if len(pending) < 6 {
			return 0
		}
		recorder.events = append(recorder.events, Event{AtMillis: recorder.elapsed(), Kind: "pointer",
			Buttons: int(pending[1]), X: int(binary.BigEndian.Uint16(pending[2:4])),
			Y: int(binary.BigEndian.Uint16(pending[4:6]))})
		return 6
	case 6:
		if len(pending) < 8 {
			return 0
		}
		length := int32(binary.BigEndian.Uint32(pending[4:8]))
		if length < 0 {
			length = -length
		}
		return fixedLength(pending, 8+int(length))
	case 248:
		if len(pending) < 9 {
			return 0
		}
		return fixedLength(pending, 9+int(pending[8]))
	case 250:
		return fixedLength(pending, 4)
	case 251:
		if len(pending) < 8 {
			return 0
		}
		return fixedLength(pending, 8+16*int(pending[6]))
	case 255:
		return recorder.qemuMessage(pending)
	}
	return recorder.lose(fmt.Sprintf("client message type %d is not recorded", pending[0]))
}

// qemuMessage handles QEMU's extended key events, which carry the keysym plus a scancode.
func (recorder *Recorder) qemuMessage(pending []byte) int {
	if len(pending) < 2 {
		return 0
	}
	if pending[1] != 0 {
		return recorder.lose(fmt.Sprintf("QEMU message subtype %d is not recorded", pending[1]))
	}
	if len(pending) < 12 {
		return 0
	}
	recorder.events = append(recorder.events, Event{AtMillis: recorder.elapsed(), Kind: "key",
		Keysym: binary.BigEndian.Uint32(pending[4:8]), Down: binary.BigEndian.Uint16(pending[2:4]) != 0})
	return 12
}

func fixedLength(pending []byte, length int) int {
	if len(pending) < length {
		return 0
	}
	return length
}
