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
	"io/fs"
	"math/rand/v2"
	"net"
	"os"
	"slices"
	"time"

	"rlworld/fleet/internal/vmm"
	"rlworld/fleet/internal/vmm/qemu"
)

// Screens are hashed as a grayscale thumbnail this size, the size the Linux guest agent hashes.
const (
	hashWidth  = 160
	hashHeight = 100
)

// InputDelay paces input batches the way the Linux guest agent types, so the guest drains its input queues.
const InputDelay = 12 * time.Millisecond

// guestAgentDelimiter resets the QEMU guest agent's parser and marks the start of a guest-sync-delimited reply.
const guestAgentDelimiter = 0xff

// defaultKinds are what an observation holds when the lease names none.
var defaultKinds = []string{"screenshot", "accessibility_tree"}

// computerActions are played at the hypervisor; every other action goes to the in session helper.
var computerActions = map[string]bool{"move": true, "click": true, "drag": true, "scroll": true, "type": true,
	"key": true, "wait": true}

// WindowsHost is the VMM's side of a Windows guest: its screen and input at the hypervisor and the host ends of
// its helper, guest agent and VNC sockets.
type WindowsHost interface {
	vmm.FramebufferCapturer
	SendInput(ctx context.Context, vmID string, events []map[string]any) error
	ScreenPath(vmID string) string
	HelperSocketPath(vmID string) string
	GuestAgentSocketPath(vmID string) string
	VNCSocketPath(vmID string) string
}

// UnixDialer reaches a per VM socket that needs no handshake, such as the host end of a virtio serial port.
type UnixDialer struct {
	SocketPath SocketPath
}

// Dial connects to the VM's socket with the deadline set.
func (dialer UnixDialer) Dial(ctx context.Context, vmID string, deadline time.Time) (net.Conn, *bufio.Reader, error) {
	var netDialer net.Dialer
	connection, err := netDialer.DialContext(ctx, "unix", dialer.SocketPath(vmID))
	if err != nil {
		return nil, nil, fmt.Errorf("dial %s: %w", vmID, err)
	}
	if err := connection.SetDeadline(deadline); err != nil {
		connection.Close()
		return nil, nil, err
	}
	return connection, bufio.NewReader(connection), nil
}

// WindowsChannel is the node agent's guest channel for Windows guests (DESIGN.md sections 6.1 and 6.3).
//
// Screens and input stay at the hypervisor: QMP screendumps and input events on a USB tablet, so session 0
// isolation, the UAC secure desktop or a tampering agent cannot break observation or input. The clock is set by
// the QEMU guest agent, a SYSTEM service; every clone's fresh VM generation id already reseeds Windows' RNG, so
// restored entropy is not passed on. The in session helper of the autologon user answers everything else over its
// virtio serial port in the Linux guest agent's protocol: UIA trees, Excel COM state and native calls, terminal and
// browser actions, injection and extraction. Operators take over through QEMU's own VNC server.
type WindowsChannel struct {
	host    WindowsHost
	helper  *Channel
	frames  FrameStore
	timeout time.Duration
	sleep   func(time.Duration)
}

// NewWindowsChannel returns a channel; calls without a context deadline give up after timeout and sleep paces input.
func NewWindowsChannel(host WindowsHost, frames FrameStore, timeout time.Duration,
	sleep func(time.Duration)) *WindowsChannel {
	helper := NewChannel(UnixDialer{SocketPath: host.HelperSocketPath}, nil, frames, timeout)
	return &WindowsChannel{host: host, helper: helper, frames: frames, timeout: timeout, sleep: sleep}
}

// capture takes a screendump and returns its PNG bytes and size.
func (channel *WindowsChannel) capture(ctx context.Context, vmID string) ([]byte, image.Config, error) {
	path := channel.host.ScreenPath(vmID)
	if err := channel.host.CaptureScreen(ctx, vmID, path); err != nil {
		return nil, image.Config{}, fmt.Errorf("screendump of %s: %w", vmID, err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, image.Config{}, err
	}
	config, err := png.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return nil, image.Config{}, fmt.Errorf("screendump of %s: %w", vmID, err)
	}
	return data, config, nil
}

// screenSize is the size of the screen the agent last saw: its latest screendump, or a fresh one before any.
func (channel *WindowsChannel) screenSize(ctx context.Context, vmID string) (int, int, error) {
	data, err := os.ReadFile(channel.host.ScreenPath(vmID))
	if errors.Is(err, fs.ErrNotExist) {
		data, _, err = channel.capture(ctx, vmID)
	}
	if err != nil {
		return 0, 0, err
	}
	config, err := png.DecodeConfig(bytes.NewReader(data))
	return config.Width, config.Height, err
}

// Prepare sets the guest clock through the QEMU guest agent.
func (channel *WindowsChannel) Prepare(ctx context.Context, vmID string, now time.Time, _ []byte) error {
	return channel.guestAgent(ctx, vmID, "guest-set-time", map[string]any{"time": now.UnixNano()})
}

type guestAgentReply struct {
	Return json.RawMessage `json:"return"`
	Error  *struct {
		Class       string `json:"class"`
		Description string `json:"desc"`
	} `json:"error"`
}

// readGuestAgentReply reads one reply line, dropping anything before a delimiter: a previous client's leftovers.
func readGuestAgentReply(reader *bufio.Reader) (guestAgentReply, error) {
	var reply guestAgentReply
	line, err := reader.ReadBytes('\n')
	if err != nil {
		return reply, err
	}
	if index := bytes.LastIndexByte(line, guestAgentDelimiter); index >= 0 {
		line = line[index+1:]
	}
	return reply, json.Unmarshal(line, &reply)
}

// guestAgent runs one QEMU guest agent command after the resync its protocol asks of every client: a delimiter
// byte resets the agent's parser, then guest-sync-delimited carries a fresh id. Replies before the one echoing that
// id belong to an earlier client.
func (channel *WindowsChannel) guestAgent(ctx context.Context, vmID, command string, arguments any) error {
	deadline, found := ctx.Deadline()
	if !found {
		deadline = time.Now().Add(channel.timeout)
	}
	connection, reader, err := UnixDialer{SocketPath: channel.host.GuestAgentSocketPath}.Dial(ctx, vmID, deadline)
	if err != nil {
		return err
	}
	defer connection.Close()
	syncID := rand.Int64N(firstRequestIDs)
	syncRequest, err := json.Marshal(map[string]any{"execute": "guest-sync-delimited",
		"arguments": map[string]any{"id": syncID}})
	if err != nil {
		return err
	}
	if _, err := connection.Write(append(append([]byte{guestAgentDelimiter}, syncRequest...), '\n')); err != nil {
		return err
	}
	for {
		reply, err := readGuestAgentReply(reader)
		var syntaxError *json.SyntaxError
		if errors.As(err, &syntaxError) {
			continue
		}
		if err != nil {
			return fmt.Errorf("guest agent of %s: %w", vmID, err)
		}
		var echoed int64
		if json.Unmarshal(reply.Return, &echoed) == nil && echoed == syncID {
			break
		}
	}
	commandRequest, err := json.Marshal(map[string]any{"execute": command, "arguments": arguments})
	if err != nil {
		return err
	}
	if _, err := connection.Write(append(commandRequest, '\n')); err != nil {
		return err
	}
	reply, err := readGuestAgentReply(reader)
	if err != nil {
		return fmt.Errorf("guest agent of %s: %w", vmID, err)
	}
	if reply.Error != nil {
		return fmt.Errorf("guest agent of %s: %s: %s", vmID, reply.Error.Class, reply.Error.Description)
	}
	return nil
}

type point struct {
	X int `json:"x"`
	Y int `json:"y"`
}

// inputAction holds the fields of every computer action.
type inputAction struct {
	Name       string   `json:"name"`
	X          int      `json:"x"`
	Y          int      `json:"y"`
	Button     string   `json:"button"`
	Count      int      `json:"count"`
	Modifiers  []string `json:"modifiers"`
	Path       []point  `json:"path"`
	DeltaX     int      `json:"delta_x"`
	DeltaY     int      `json:"delta_y"`
	Text       string   `json:"text"`
	Keys       []string `json:"keys"`
	DurationMS int      `json:"duration_ms"`
}

// Act plays computer actions at the hypervisor one input batch at a time and hands the rest to the helper.
func (channel *WindowsChannel) Act(ctx context.Context, vmID string, action json.RawMessage) error {
	var parsed inputAction
	if err := json.Unmarshal(action, &parsed); err != nil {
		return err
	}
	if !computerActions[parsed.Name] {
		return channel.helper.Act(ctx, vmID, action)
	}
	if parsed.Name == "wait" {
		channel.sleep(time.Duration(parsed.DurationMS) * time.Millisecond)
		return nil
	}
	batches, err := channel.inputBatches(ctx, vmID, parsed)
	if err != nil {
		return err
	}
	for index, batch := range batches {
		if index > 0 {
			channel.sleep(InputDelay)
		}
		if err := channel.host.SendInput(ctx, vmID, batch); err != nil {
			return fmt.Errorf("input to %s: %w", vmID, err)
		}
	}
	return nil
}

func (channel *WindowsChannel) inputBatches(ctx context.Context, vmID string, action inputAction) (
	[][]map[string]any, error) {
	switch action.Name {
	case "type":
		return qemu.TextBatches(action.Text)
	case "key":
		return qemu.KeyBatches(action.Keys)
	}
	width, height, err := channel.screenSize(ctx, vmID)
	if err != nil {
		return nil, err
	}
	button := action.Button
	if button == "" {
		button = "left"
	}
	switch action.Name {
	case "move":
		return qemu.MoveBatches(action.X, action.Y, width, height), nil
	case "click":
		return qemu.ClickBatches(action.X, action.Y, width, height, button, action.Count, action.Modifiers)
	case "drag":
		if len(action.Path) < 2 {
			return nil, errors.New("a drag needs at least two points")
		}
		points := make([][2]int, 0, len(action.Path))
		for _, step := range action.Path {
			points = append(points, [2]int{step.X, step.Y})
		}
		return qemu.DragBatches(points, width, height, button), nil
	}
	return qemu.ScrollBatches(action.X, action.Y, width, height, action.DeltaX, action.DeltaY), nil
}

// rgbPixels exposes the pixels of the image types PNG decoding returns, so hashing skips a call per pixel.
func rgbPixels(picture image.Image) ([]uint8, int) {
	switch typed := picture.(type) {
	case *image.RGBA:
		return typed.Pix, typed.Stride
	case *image.NRGBA:
		return typed.Pix, typed.Stride
	}
	return nil, 0
}

// coarseHash hashes a grayscale thumbnail whose every cell averages its area of the screen, as the Linux guest
// agent's resized thumbnail does, so settle detection sees what changed and ignores where exactly.
func coarseHash(picture image.Image) string {
	bounds := picture.Bounds()
	sums := make([]uint64, hashWidth*hashHeight)
	counts := make([]uint64, hashWidth*hashHeight)
	pixels, stride := rgbPixels(picture)
	for y := range bounds.Dy() {
		row := y * hashHeight / bounds.Dy() * hashWidth
		for x := range bounds.Dx() {
			var red, green, blue uint32
			if pixels != nil {
				offset := y*stride + x*4
				red, green, blue = uint32(pixels[offset]), uint32(pixels[offset+1]), uint32(pixels[offset+2])
			} else {
				wideRed, wideGreen, wideBlue, _ := picture.At(bounds.Min.X+x, bounds.Min.Y+y).RGBA()
				red, green, blue = wideRed>>8, wideGreen>>8, wideBlue>>8
			}
			cell := row + x*hashWidth/bounds.Dx()
			sums[cell] += uint64((19595*red + 38470*green + 7471*blue + 1<<15) >> 16)
			counts[cell]++
		}
	}
	thumbnail := make([]byte, len(sums))
	for cell, sum := range sums {
		if counts[cell] > 0 {
			thumbnail[cell] = byte(sum / counts[cell])
		}
	}
	digest := sha256.Sum256(thumbnail)
	return hex.EncodeToString(digest[:])
}

// ScreenHash hashes a fresh screendump for settle detection.
func (channel *WindowsChannel) ScreenHash(ctx context.Context, vmID string) (string, error) {
	data, _, err := channel.capture(ctx, vmID)
	if err != nil {
		return "", err
	}
	picture, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		return "", fmt.Errorf("screendump of %s: %w", vmID, err)
	}
	return coarseHash(picture), nil
}

// Observe stores a screendump as the screenshot blob and asks the helper for every other kind, as well as for what
// the last actions reported: terminal output, native call results and chat posts.
func (channel *WindowsChannel) Observe(ctx context.Context, vmID string, kinds []string) (json.RawMessage, error) {
	if len(kinds) == 0 {
		kinds = defaultKinds
	}
	var screenshot []byte
	if slices.Contains(kinds, "screenshot") {
		data, config, err := channel.capture(ctx, vmID)
		if err != nil {
			return nil, err
		}
		digest, err := channel.frames.Put(data)
		if err != nil {
			return nil, fmt.Errorf("store screenshot of %s: %w", vmID, err)
		}
		if screenshot, err = json.Marshal(screenshotRef{URI: "blob:sha256:" + digest, Width: config.Width,
			Height: config.Height, ImageFormat: "png", SHA256: digest}); err != nil {
			return nil, err
		}
	}
	helperKinds := []string{}
	for _, kind := range kinds {
		if kind != "screenshot" {
			helperKinds = append(helperKinds, kind)
		}
	}
	observed, err := channel.helper.Observe(ctx, vmID, helperKinds)
	if err != nil {
		return nil, err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(observed, &fields); err != nil {
		return nil, err
	}
	if screenshot != nil {
		fields["screenshot"] = screenshot
	}
	return json.Marshal(fields)
}

// Inject hands world state, task setup, entitlements and deliveries to the helper.
func (channel *WindowsChannel) Inject(ctx context.Context, vmID string, payload json.RawMessage) error {
	return channel.helper.Inject(ctx, vmID, payload)
}

// Extract reads app state, Excel's open workbooks among it, from the helper.
func (channel *WindowsChannel) Extract(ctx context.Context, vmID string, apps []string) (json.RawMessage, error) {
	return channel.helper.Extract(ctx, vmID, apps)
}

// DialVNC opens QEMU's VNC server for a takeover; the connection has no deadline because teleop is open ended.
func (channel *WindowsChannel) DialVNC(ctx context.Context, vmID string) (net.Conn, *bufio.Reader, error) {
	var netDialer net.Dialer
	connection, err := netDialer.DialContext(ctx, "unix", channel.host.VNCSocketPath(vmID))
	if err != nil {
		return nil, nil, fmt.Errorf("dial the VNC server of %s: %w", vmID, err)
	}
	return connection, bufio.NewReader(connection), nil
}
