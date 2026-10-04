package guest

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/png"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeWindowsHost plays QEMU: screendumps of the current screen and recorded input batches.
type fakeWindowsHost struct {
	mu           sync.Mutex
	runDir       string
	screen       image.Image
	captures     int
	batches      [][]map[string]any
	captureError error
	inputError   error
}

func (host *fakeWindowsHost) CaptureScreen(_ context.Context, _, outputPath string) error {
	host.mu.Lock()
	defer host.mu.Unlock()
	if host.captureError != nil {
		return host.captureError
	}
	host.captures++
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, host.screen); err != nil {
		return err
	}
	return os.WriteFile(outputPath, encoded.Bytes(), 0o600)
}

func (host *fakeWindowsHost) SendInput(_ context.Context, _ string, events []map[string]any) error {
	host.mu.Lock()
	defer host.mu.Unlock()
	if host.inputError != nil {
		return host.inputError
	}
	host.batches = append(host.batches, events)
	return nil
}

func (host *fakeWindowsHost) setScreen(screen image.Image) {
	host.mu.Lock()
	defer host.mu.Unlock()
	host.screen = screen
}

func (host *fakeWindowsHost) ScreenPath(vmID string) string {
	return filepath.Join(host.runDir, vmID+".screen.png")
}

func (host *fakeWindowsHost) HelperSocketPath(vmID string) string {
	return filepath.Join(host.runDir, vmID+".helper")
}

func (host *fakeWindowsHost) GuestAgentSocketPath(vmID string) string {
	return filepath.Join(host.runDir, vmID+".qga")
}

func (host *fakeWindowsHost) VNCSocketPath(vmID string) string {
	return filepath.Join(host.runDir, vmID+".vnc")
}

// fakeHelper plays the in session helper behind the host end of its virtio serial port: no handshake, one JSON
// request line and one reply line per call.
type fakeHelper struct {
	mu       sync.Mutex
	listener net.Listener
	requests []map[string]any
	replies  map[string]string
}

func (fake *fakeHelper) serve() {
	for {
		connection, err := fake.listener.Accept()
		if err != nil {
			return
		}
		fake.answer(connection)
	}
}

func (fake *fakeHelper) answer(connection net.Conn) {
	defer connection.Close()
	line, err := bufio.NewReader(connection).ReadBytes('\n')
	if err != nil {
		return
	}
	var request map[string]any
	json.Unmarshal(line, &request)
	fake.mu.Lock()
	fake.requests = append(fake.requests, request)
	reply, found := fake.replies[request["method"].(string)]
	fake.mu.Unlock()
	if !found {
		reply = `{"id": ID, "result": {}}`
	}
	connection.Write([]byte(withID(reply, request) + "\n"))
}

func (fake *fakeHelper) recorded() []map[string]any {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	return append([]map[string]any{}, fake.requests...)
}

// fakeGuestAgent plays the QEMU guest agent: an earlier client's leftovers, then the delimited sync reply, then the
// command's reply.
type fakeGuestAgent struct {
	mu       sync.Mutex
	listener net.Listener
	lines    [][]byte
	reply    string
}

func (fake *fakeGuestAgent) serve() {
	for {
		connection, err := fake.listener.Accept()
		if err != nil {
			return
		}
		fake.answer(connection)
	}
}

func (fake *fakeGuestAgent) answer(connection net.Conn) {
	defer connection.Close()
	reader := bufio.NewReader(connection)
	syncLine, err := reader.ReadBytes('\n')
	if err != nil {
		return
	}
	var syncRequest struct {
		Arguments struct {
			ID int64 `json:"id"`
		} `json:"arguments"`
	}
	json.Unmarshal(bytes.TrimPrefix(syncLine, []byte{guestAgentDelimiter}), &syncRequest)
	connection.Write([]byte("{\"return\": {}}\nleftover from an earlier client\n{\"return\": 7}\n"))
	connection.Write([]byte(fmt.Sprintf("half a line\xff{\"return\": %d}\n", syncRequest.Arguments.ID)))
	commandLine, err := reader.ReadBytes('\n')
	fake.mu.Lock()
	fake.lines = append(fake.lines, syncLine, commandLine)
	reply := fake.reply
	fake.mu.Unlock()
	if err == nil {
		connection.Write([]byte(reply + "\n"))
	}
}

// sleeper records the pauses the channel asks for instead of sleeping.
type sleeper struct {
	mu        sync.Mutex
	durations []time.Duration
}

func (recorder *sleeper) sleep(duration time.Duration) {
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	recorder.durations = append(recorder.durations, duration)
}

type windowsFixture struct {
	host    *fakeWindowsHost
	helper  *fakeHelper
	agent   *fakeGuestAgent
	sleeps  *sleeper
	channel *WindowsChannel
	frames  string
}

func (fixture *windowsFixture) close() {
	fixture.helper.listener.Close()
	fixture.agent.listener.Close()
	os.RemoveAll(fixture.host.runDir)
}

func filledScreen(width, height int, shade uint8) *image.RGBA {
	screen := image.NewRGBA(image.Rect(0, 0, width, height))
	for offset := 0; offset < len(screen.Pix); offset += 4 {
		screen.Pix[offset], screen.Pix[offset+1], screen.Pix[offset+2], screen.Pix[offset+3] = shade, shade, shade, 255
	}
	return screen
}

func startWindowsFixture(t *testing.T, replies map[string]string) *windowsFixture {
	t.Helper()
	runDir, err := os.MkdirTemp("/tmp", "win")
	if err != nil {
		t.Fatal(err)
	}
	host := &fakeWindowsHost{runDir: runDir, screen: filledScreen(1281, 801, 0)}
	helperListener, err := net.Listen("unix", host.HelperSocketPath("vm_1"))
	if err != nil {
		os.RemoveAll(runDir)
		t.Fatal(err)
	}
	agentListener, err := net.Listen("unix", host.GuestAgentSocketPath("vm_1"))
	if err != nil {
		helperListener.Close()
		os.RemoveAll(runDir)
		t.Fatal(err)
	}
	fixture := &windowsFixture{host: host, helper: &fakeHelper{listener: helperListener, replies: replies},
		agent: &fakeGuestAgent{listener: agentListener, reply: `{"return": {}}`}, sleeps: &sleeper{},
		frames: t.TempDir()}
	t.Cleanup(fixture.close)
	go fixture.helper.serve()
	go fixture.agent.serve()
	fixture.channel = NewWindowsChannel(host, NewFilesystemFrames(fixture.frames), 2*time.Second,
		fixture.sleeps.sleep)
	return fixture
}

func pressSign(down bool) string {
	if down {
		return "+"
	}
	return "-"
}

// summarized renders one batch as short words such as "ctrl+", "x=16383" or "left-".
func summarized(batch []map[string]any) string {
	words := []string{}
	for _, event := range batch {
		data := event["data"].(map[string]any)
		switch event["type"] {
		case "key":
			words = append(words, fmt.Sprint(data["key"].(map[string]any)["data"])+pressSign(data["down"].(bool)))
		case "btn":
			words = append(words, fmt.Sprint(data["button"])+pressSign(data["down"].(bool)))
		default:
			words = append(words, fmt.Sprintf("%s=%d", data["axis"], data["value"]))
		}
	}
	return strings.Join(words, " ")
}

func (fixture *windowsFixture) played(t *testing.T, action string) []string {
	t.Helper()
	fixture.host.mu.Lock()
	fixture.host.batches = nil
	fixture.host.mu.Unlock()
	if err := fixture.channel.Act(context.Background(), "vm_1", json.RawMessage(action)); err != nil {
		t.Fatalf("act %s: %v", action, err)
	}
	fixture.host.mu.Lock()
	defer fixture.host.mu.Unlock()
	played := []string{}
	for _, batch := range fixture.host.batches {
		played = append(played, summarized(batch))
	}
	return played
}

func expectPlayed(t *testing.T, played, want []string) {
	t.Helper()
	if !reflect.DeepEqual(played, want) {
		t.Fatalf("played %q, want %q", played, want)
	}
}

func TestComputerActionsArePlayedAtTheHypervisorInPacedBatches(t *testing.T) {
	fixture := startWindowsFixture(t, nil)
	expectPlayed(t, fixture.played(t, `{"name": "click", "x": 640, "y": 400, "count": 2, "modifiers": ["ctrl"]}`),
		[]string{"ctrl+", "x=16383 y=16383", "left+", "left-", "left+", "left-", "ctrl-"})
	if fixture.host.captures != 1 {
		t.Fatalf("the first pointer action reads the screen size from one screendump, took %d", fixture.host.captures)
	}
	if len(fixture.sleeps.durations) != 6 || fixture.sleeps.durations[0] != InputDelay {
		t.Fatalf("batches are paced: %v", fixture.sleeps.durations)
	}
	expectPlayed(t, fixture.played(t, `{"name": "key", "keys": ["ctrl", "s"]}`), []string{"ctrl+ s+", "s- ctrl-"})
	expectPlayed(t, fixture.played(t, `{"name": "type", "text": "Hi"}`), []string{"shift+ h+ h- shift-", "i+ i-"})
	expectPlayed(t, fixture.played(t, `{"name": "move", "x": 1280, "y": 0}`), []string{"x=32767 y=0"})
	expectPlayed(t, fixture.played(t, `{"name": "drag", "path": [{"x": 0, "y": 0}, {"x": 1280, "y": 800}],
		"button": "middle"}`), []string{"x=0 y=0", "middle+", "x=32767 y=32767", "middle-"})
	expectPlayed(t, fixture.played(t, `{"name": "scroll", "x": 0, "y": 800, "delta_y": -2}`),
		[]string{"x=0 y=32767", "wheel-up+ wheel-up-", "wheel-up+ wheel-up-"})
	if fixture.host.captures != 1 {
		t.Fatalf("later pointer actions reuse the last screendump, took %d", fixture.host.captures)
	}
	fixture.sleeps.durations = nil
	expectPlayed(t, fixture.played(t, `{"name": "wait", "duration_ms": 250}`), []string{})
	if !reflect.DeepEqual(fixture.sleeps.durations, []time.Duration{250 * time.Millisecond}) {
		t.Fatalf("wait sleeps on the host: %v", fixture.sleeps.durations)
	}
	if requests := fixture.helper.recorded(); len(requests) != 0 {
		t.Fatalf("computer actions never reach the helper: %v", requests)
	}
}

func TestPointerActionsScaleToTheScreenTheAgentLastSaw(t *testing.T) {
	fixture := startWindowsFixture(t, nil)
	fixture.host.setScreen(filledScreen(1920, 1080, 0))
	if _, err := fixture.channel.ScreenHash(context.Background(), "vm_1"); err != nil {
		t.Fatal(err)
	}
	expectPlayed(t, fixture.played(t, `{"name": "move", "x": 1919, "y": 1079}`), []string{"x=32767 y=32767"})
	expectPlayed(t, fixture.played(t, `{"name": "move", "x": 960, "y": 540}`),
		[]string{fmt.Sprintf("x=%d y=%d", 960*0x7fff/1919, 540*0x7fff/1079)})
}

func TestBadComputerActionsFailBeforeAnyInput(t *testing.T) {
	fixture := startWindowsFixture(t, nil)
	for _, action := range []string{
		`{"name": "drag", "path": [{"x": 1, "y": 1}]}`,
		`{"name": "type", "text": "café"}`,
		`{"name": "key", "keys": ["hyperspace"]}`,
		`{"name": "click", "x": 1, "y": 1, "modifiers": ["hyper"]}`,
		`not json`,
	} {
		if err := fixture.channel.Act(context.Background(), "vm_1", json.RawMessage(action)); err == nil {
			t.Fatalf("%s must fail", action)
		}
	}
	if len(fixture.host.batches) != 0 || len(fixture.helper.recorded()) != 0 {
		t.Fatalf("nothing was sent: %v %v", fixture.host.batches, fixture.helper.recorded())
	}
	fixture.host.inputError = errors.New("qmp went away")
	err := fixture.channel.Act(context.Background(), "vm_1", json.RawMessage(`{"name": "key", "keys": ["enter"]}`))
	if err == nil || !strings.Contains(err.Error(), "qmp went away") {
		t.Fatalf("input failures surface: %v", err)
	}
	fixture.host.captureError = errors.New("no framebuffer")
	err = fixture.channel.Act(context.Background(), "vm_2", json.RawMessage(`{"name": "move", "x": 1, "y": 1}`))
	if err == nil || !strings.Contains(err.Error(), "no framebuffer") {
		t.Fatalf("a pointer action without a screen size fails: %v", err)
	}
}

func TestOtherActionsGoToTheHelper(t *testing.T) {
	fixture := startWindowsFixture(t, map[string]string{
		"act": `{"id": ID, "error": {"code": "unsupported_action", "message": "no app named word"}}`,
	})
	action := `{"name": "native_call", "app": "word", "method": "open", "arguments": {}}`
	err := fixture.channel.Act(context.Background(), "vm_1", json.RawMessage(action))
	var failure *Error
	if !errors.As(err, &failure) || failure.Code != "unsupported_action" {
		t.Fatalf("helper errors surface with their code: %v", err)
	}
	requests := fixture.helper.recorded()
	sent, _ := json.Marshal(requests[0]["params"].(map[string]any)["action"])
	var want, got any
	json.Unmarshal([]byte(action), &want)
	json.Unmarshal(sent, &got)
	if requests[0]["method"] != "act" || !reflect.DeepEqual(got, want) {
		t.Fatalf("helper request: %v", requests[0])
	}
	if len(fixture.host.batches) != 0 || fixture.host.captures != 0 {
		t.Fatal("helper actions send no input and take no screendump")
	}
}

func TestScreenHashSeesWhatChangedButNotWhereExactly(t *testing.T) {
	fixture := startWindowsFixture(t, nil)
	ctx := context.Background()
	first, err := fixture.channel.ScreenHash(ctx, "vm_1")
	if err != nil {
		t.Fatal(err)
	}
	again, _ := fixture.channel.ScreenHash(ctx, "vm_1")
	blank := sha256Hex(make([]byte, hashWidth*hashHeight))
	if first != again || first != blank {
		t.Fatalf("a still black screen hashes the same, to a black thumbnail: %s %s", first, again)
	}
	changed := filledScreen(1281, 801, 0)
	for y := 400; y < 412; y++ {
		for x := 600; x < 612; x++ {
			changed.Pix[changed.PixOffset(x, y)] = 255
		}
	}
	fixture.host.setScreen(changed)
	if hash, _ := fixture.channel.ScreenHash(ctx, "vm_1"); hash == first {
		t.Fatal("a small change on screen must change the hash")
	}
	gray := image.NewGray(image.Rect(0, 0, 320, 200))
	colored := filledScreen(320, 200, 0)
	for offset := range gray.Pix {
		gray.Pix[offset] = uint8(offset % 251)
		colored.Pix[offset*4], colored.Pix[offset*4+1], colored.Pix[offset*4+2] = gray.Pix[offset],
			gray.Pix[offset], gray.Pix[offset]
	}
	if coarseHash(gray) != coarseHash(colored) {
		t.Fatal("decoded pixels and the generic path must agree")
	}
	fixture.host.captureError = errors.New("guest paused")
	if _, err := fixture.channel.ScreenHash(ctx, "vm_1"); err == nil {
		t.Fatal("a failed screendump fails the hash")
	}
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func TestObserveStoresTheScreendumpAndAsksTheHelperForTheRest(t *testing.T) {
	fixture := startWindowsFixture(t, map[string]string{
		"observe": `{"id": ID, "result": {"step_index": 0, "accessibility": [{"node_id": "0", "role": "frame", ` +
			`"depth": 0}], "terminal": {"stdout": "ok", "stderr": "", "exit_code": 0}}}`,
	})
	raw, err := fixture.channel.Observe(context.Background(), "vm_1", []string{"screenshot", "accessibility_tree",
		"dom"})
	if err != nil {
		t.Fatal(err)
	}
	var observation map[string]any
	json.Unmarshal(raw, &observation)
	screenshot := observation["screenshot"].(map[string]any)
	digest := screenshot["sha256"].(string)
	if screenshot["uri"] != "blob:sha256:"+digest || screenshot["image_format"] != "png" ||
		screenshot["width"].(float64) != 1281 || screenshot["height"].(float64) != 801 {
		t.Fatalf("screenshot ref: %v", screenshot)
	}
	stored, err := os.ReadFile(filepath.Join(fixture.frames, "blobs", digest[:2], digest))
	if err != nil || sha256Hex(stored) != digest {
		t.Fatalf("the screendump is the stored blob: %v", err)
	}
	if _, err := png.Decode(bytes.NewReader(stored)); err != nil {
		t.Fatalf("blobs hold the PNG: %v", err)
	}
	if observation["terminal"].(map[string]any)["stdout"] != "ok" || len(observation["accessibility"].([]any)) != 1 {
		t.Fatalf("helper fields stay: %v", observation)
	}
	for _, kinds := range [][]string{{"screenshot"}, nil} {
		if _, err := fixture.channel.Observe(context.Background(), "vm_1", kinds); err != nil {
			t.Fatal(err)
		}
	}
	asked := [][]any{}
	for _, request := range fixture.helper.recorded() {
		asked = append(asked, request["params"].(map[string]any)["kinds"].([]any))
	}
	want := [][]any{{"accessibility_tree", "dom"}, {}, {"accessibility_tree"}}
	if !reflect.DeepEqual(asked, want) {
		t.Fatalf("helper kinds %v, want %v", asked, want)
	}
}

func TestObserveFailsWhenTheHelperOrTheScreenDoes(t *testing.T) {
	fixture := startWindowsFixture(t, map[string]string{
		"observe": `{"id": ID, "error": {"code": "internal", "message": "UIA timed out"}}`,
	})
	if _, err := fixture.channel.Observe(context.Background(), "vm_1", nil); err == nil ||
		!strings.Contains(err.Error(), "UIA timed out") {
		t.Fatalf("helper failure: %v", err)
	}
	fixture.host.captureError = errors.New("guest paused")
	if _, err := fixture.channel.Observe(context.Background(), "vm_1", []string{"screenshot"}); err == nil {
		t.Fatal("a failed screendump fails the observation")
	}
}

func TestPrepareSetsTheClockThroughTheGuestAgentAfterAResync(t *testing.T) {
	fixture := startWindowsFixture(t, nil)
	worldTime := time.Date(2026, 3, 14, 9, 0, 5, 7, time.UTC)
	if err := fixture.channel.Prepare(context.Background(), "vm_1", worldTime, []byte{1, 2, 3}); err != nil {
		t.Fatal(err)
	}
	fixture.agent.mu.Lock()
	lines := fixture.agent.lines
	fixture.agent.mu.Unlock()
	if len(lines) != 2 || lines[0][0] != guestAgentDelimiter {
		t.Fatalf("a delimiter byte opens every session: %q", lines)
	}
	var syncRequest, setTime map[string]any
	json.Unmarshal(lines[0][1:], &syncRequest)
	decoder := json.NewDecoder(bytes.NewReader(lines[1]))
	decoder.UseNumber()
	decoder.Decode(&setTime)
	if syncRequest["execute"] != "guest-sync-delimited" || setTime["execute"] != "guest-set-time" {
		t.Fatalf("commands: %v %v", syncRequest, setTime)
	}
	if nanoseconds := setTime["arguments"].(map[string]any)["time"].(json.Number).String(); nanoseconds !=
		fmt.Sprint(worldTime.UnixNano()) {
		t.Fatalf("set time to %s", nanoseconds)
	}
	if len(fixture.helper.recorded()) != 0 {
		t.Fatal("restored entropy never reaches the guest; the VM generation id reseeds it")
	}
	fixture.agent.mu.Lock()
	fixture.agent.reply = `{"error": {"class": "GenericError", "desc": "privilege not held"}}`
	fixture.agent.mu.Unlock()
	err := fixture.channel.Prepare(context.Background(), "vm_1", worldTime, nil)
	if err == nil || !strings.Contains(err.Error(), "GenericError: privilege not held") {
		t.Fatalf("guest agent errors surface: %v", err)
	}
	if err := fixture.channel.Prepare(context.Background(), "vm_2", worldTime, nil); err == nil {
		t.Fatal("a VM without a guest agent socket cannot be prepared")
	}
}

func TestInjectAndExtractGoToTheHelper(t *testing.T) {
	fixture := startWindowsFixture(t, map[string]string{
		"extract": `{"id": ID, "result": {"excel": {"workbooks": {}}}}`,
	})
	payload := json.RawMessage(`{"apps": ["excel"], "files": {"Documents/a.txt": {"text": "hi"}}}`)
	if err := fixture.channel.Inject(context.Background(), "vm_1", payload); err != nil {
		t.Fatal(err)
	}
	state, err := fixture.channel.Extract(context.Background(), "vm_1", []string{"excel"})
	if err != nil || string(state) != `{"excel": {"workbooks": {}}}` {
		t.Fatalf("extract: %s %v", state, err)
	}
	requests := fixture.helper.recorded()
	if requests[0]["method"] != "inject" || requests[0]["params"].(map[string]any)["apps"].([]any)[0] != "excel" {
		t.Fatalf("inject request: %v", requests[0])
	}
	if requests[1]["method"] != "extract" || requests[1]["params"].(map[string]any)["apps"].([]any)[0] != "excel" {
		t.Fatalf("extract request: %v", requests[1])
	}
}

// fakeVNCServer plays QEMU's VNC server on its unix socket.
type fakeVNCServer struct {
	listener net.Listener
}

func (server *fakeVNCServer) serve() {
	for {
		connection, err := server.listener.Accept()
		if err != nil {
			return
		}
		connection.Write([]byte("RFB 003.008\n"))
		echo(bufio.NewReader(connection), connection)
		connection.Close()
	}
}

func (server *fakeVNCServer) close() {
	server.listener.Close()
}

func TestTakeoverDialsQEMUsVNCSocket(t *testing.T) {
	fixture := startWindowsFixture(t, nil)
	listener, err := net.Listen("unix", fixture.host.VNCSocketPath("vm_1"))
	if err != nil {
		t.Fatal(err)
	}
	server := &fakeVNCServer{listener: listener}
	t.Cleanup(server.close)
	go server.serve()
	connection, reader, err := fixture.channel.DialVNC(context.Background(), "vm_1")
	if err != nil {
		t.Fatal(err)
	}
	readVNC(t, connection, reader)
	if _, _, err := fixture.channel.DialVNC(context.Background(), "vm_2"); err == nil {
		t.Fatal("a VM without a VNC socket cannot be taken over")
	}
}
