package fleetd

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"time"

	"rlworld/fleet/internal/takeover"
	"rlworld/fleet/internal/websocket"
)

// Takeover lets an operator teleop a leased env through noVNC (DESIGN.md section 6.14).
//
// Starting one marks the lease taken over, so the rollout worker's calls are
// refused until it ends while the env keeps running for the operator. It opens a
// session whose id is the only credential the relay accepts: random, usable
// once and only within the connect window. The relay records the operator's
// input; ending the takeover closes the relay, pauses the env again and
// returns what was recorded for the episode's demonstration.

const (
	takeoverConnectWindow = 2 * time.Minute
	guestDialTimeout      = 30 * time.Second
	maxRelayMessageBytes  = 1 << 20
	relayBufferBytes      = 32 * 1024
)

var (
	// ErrTakenOver means an operator drives the env; the worker waits until the takeover ends.
	ErrTakenOver = errors.New("an operator has taken over this env")
	// ErrNoTakeover means the lease has no takeover to end.
	ErrNoTakeover = errors.New("the lease is not taken over")
	// ErrUnknownTakeover means no session with that id waits for its relay.
	ErrUnknownTakeover = errors.New("no takeover session with that id is waiting")
)

// TakeoverInfo is what the console needs to open noVNC.
type TakeoverInfo struct {
	SessionID string    `json:"session_id"`
	RelayPath string    `json:"relay_path"`
	ExpiresAt time.Time `json:"expires_at"`
}

// TakeoverRecord is the operator's input, for the episode's demonstration; a failed pause is reported, not hidden.
type TakeoverRecord struct {
	Events     []takeover.Event `json:"events"`
	LostReason string           `json:"lost_reason"`
	StartedAt  time.Time        `json:"started_at"`
	EndedAt    time.Time        `json:"ended_at"`
	PauseError string           `json:"pause_error,omitempty"`
}

type takeoverSession struct {
	id        string
	leaseID   string
	startedAt time.Time
	expiresAt time.Time
	connected bool
	recorder  *takeover.Recorder
	closers   []io.Closer
}

func newSessionID() (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return hex.EncodeToString(raw), nil
}

func (service *Service) takenOver(bound *binding) bool {
	service.mu.Lock()
	defer service.mu.Unlock()
	return bound.takeover != nil
}

// StartTakeover hands the env to an operator.
func (service *Service) StartTakeover(ctx context.Context, leaseID string) (TakeoverInfo, error) {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return TakeoverInfo{}, err
	}
	node, err := service.nodeFor(bound)
	if err != nil {
		return TakeoverInfo{}, err
	}
	sessionID, err := newSessionID()
	if err != nil {
		return TakeoverInfo{}, err
	}
	now := service.now()
	session := &takeoverSession{id: sessionID, leaseID: leaseID, startedAt: now,
		expiresAt: now.Add(takeoverConnectWindow), recorder: takeover.NewRecorder(now, service.now)}
	service.mu.Lock()
	switch {
	case bound.takeover != nil:
		err = ErrTakenOver
	case bound.frozen:
		err = ErrInputFrozen
	default:
		bound.takeover = session
		service.takeovers[sessionID] = session
	}
	envID := bound.envID
	service.mu.Unlock()
	if err != nil {
		return TakeoverInfo{}, err
	}
	if err := node.Resume(ctx, envID); err != nil {
		service.dropTakeover(bound, session)
		return TakeoverInfo{}, err
	}
	return TakeoverInfo{SessionID: sessionID, RelayPath: "/v1/takeover/" + sessionID, ExpiresAt: session.expiresAt},
		nil
}

// dropTakeover forgets a session and closes its relay; the connections close outside the lock.
func (service *Service) dropTakeover(bound *binding, session *takeoverSession) {
	service.mu.Lock()
	delete(service.takeovers, session.id)
	if bound.takeover == session {
		bound.takeover = nil
	}
	closers := session.closers
	session.closers = nil
	service.mu.Unlock()
	for _, closer := range closers {
		closer.Close()
	}
}

// EndTakeover gives the env back: the relay closes, the env pauses and the recorded input comes back.
func (service *Service) EndTakeover(ctx context.Context, leaseID string) (TakeoverRecord, error) {
	bound, err := service.lookup(leaseID)
	if err != nil {
		return TakeoverRecord{}, err
	}
	service.mu.Lock()
	session := bound.takeover
	envID := bound.envID
	service.mu.Unlock()
	if session == nil {
		return TakeoverRecord{}, ErrNoTakeover
	}
	service.dropTakeover(bound, session)
	events, lostReason := session.recorder.Events()
	record := TakeoverRecord{Events: events, LostReason: lostReason, StartedAt: session.startedAt,
		EndedAt: service.now()}
	node, err := service.nodeFor(bound)
	if err == nil {
		err = node.Pause(ctx, envID)
	}
	if err != nil {
		record.PauseError = err.Error()
	}
	return record, nil
}

// connectTakeover claims a waiting session for its relay.
func (service *Service) connectTakeover(sessionID string) (*takeoverSession, Node, string, error) {
	service.mu.Lock()
	defer service.mu.Unlock()
	session, found := service.takeovers[sessionID]
	if !found || session.connected || service.now().After(session.expiresAt) {
		return nil, nil, "", ErrUnknownTakeover
	}
	bound, found := service.bindings[session.leaseID]
	if !found || bound.takeover != session {
		return nil, nil, "", ErrUnknownTakeover
	}
	node, found := service.nodes[bound.hostID]
	if !found {
		return nil, nil, "", ErrUnknownHost
	}
	session.connected = true
	return session, node, bound.envID, nil
}

// attachTakeover lets EndTakeover close the relay; a session that already ended closes it at once.
func (service *Service) attachTakeover(session *takeoverSession, closers ...io.Closer) {
	service.mu.Lock()
	_, live := service.takeovers[session.id]
	if live {
		session.closers = append(session.closers, closers...)
	}
	service.mu.Unlock()
	if !live {
		for _, closer := range closers {
			closer.Close()
		}
	}
}

func relayGuestToBrowser(guest io.Reader, browser *websocket.Conn) {
	buffer := make([]byte, relayBufferBytes)
	for {
		count, err := guest.Read(buffer)
		if count > 0 && browser.WriteBinary(buffer[:count]) != nil {
			break
		}
		if err != nil {
			break
		}
	}
	browser.Close()
}

func relayBrowserToGuest(browser *websocket.Conn, guest io.Writer, recorder *takeover.Recorder) {
	for {
		message, err := browser.ReadMessage()
		if err != nil {
			return
		}
		recorder.Feed(message)
		if _, err := guest.Write(message); err != nil {
			return
		}
	}
}

// takeoverRelay joins the operator's noVNC to the env's screen; the session id is the credential.
func (api *httpAPI) takeoverRelay(writer http.ResponseWriter, request *http.Request) {
	session, node, envID, err := api.service.connectTakeover(request.PathValue("sessionID"))
	if err != nil {
		writeError(writer, err)
		return
	}
	dialContext, cancel := context.WithTimeout(context.Background(), guestDialTimeout)
	guestConnection, guestReader, err := node.DialVNC(dialContext, envID)
	cancel()
	if err != nil {
		writeError(writer, err)
		return
	}
	browser, err := websocket.Upgrade(writer, request, maxRelayMessageBytes)
	if err != nil {
		guestConnection.Close()
		return
	}
	api.service.attachTakeover(session, browser, guestConnection)
	go relayGuestToBrowser(guestReader, browser)
	relayBrowserToGuest(browser, guestConnection, session.recorder)
	browser.Close()
	guestConnection.Close()
}

func (api *httpAPI) startTakeover(writer http.ResponseWriter, request *http.Request) {
	info, err := api.service.StartTakeover(request.Context(), request.PathValue("leaseID"))
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusCreated, info)
}

func (api *httpAPI) endTakeover(writer http.ResponseWriter, request *http.Request) {
	record, err := api.service.EndTakeover(request.Context(), request.PathValue("leaseID"))
	if err != nil {
		writeError(writer, err)
		return
	}
	writeJSON(writer, http.StatusOK, record)
}
