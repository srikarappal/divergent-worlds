package qemu

import (
	"fmt"
	"slices"
	"strings"
	"unicode"
)

// absoluteAxisMax is the range QEMU's absolute pointer axes use.
const absoluteAxisMax = 0x7fff

// qcodes maps canonical key names (the rlworld action vocabulary) to QEMU qcodes.
var qcodes = map[string]string{
	"ctrl": "ctrl", "alt": "alt", "shift": "shift", "meta": "meta_l",
	"enter": "ret", "tab": "tab", "escape": "esc", "backspace": "backspace", "delete": "delete",
	"insert": "insert", "space": "spc", "up": "up", "down": "down", "left": "left", "right": "right",
	"home": "home", "end": "end", "page_up": "pgup", "page_down": "pgdn", "caps_lock": "caps_lock",
	"print_screen": "print", "-": "minus", "=": "equal", ",": "comma", ".": "dot", "/": "slash",
	";": "semicolon", "'": "apostrophe", "[": "bracket_left", "]": "bracket_right", "\\": "backslash",
	"`": "grave_accent",
}

// shiftedCharacters are what a US keyboard types with shift held, by the key that types them.
var shiftedCharacters = map[rune]string{
	'!': "1", '@': "2", '#': "3", '$': "4", '%': "5", '^': "6", '&': "7", '*': "8", '(': "9", ')': "0",
	'_': "-", '+': "=", '{': "[", '}': "]", '|': "\\", ':': ";", '"': "'", '<': ",", '>': ".", '?': "/",
	'~': "`",
}

// QCode returns the QEMU qcode for a canonical key name.
func QCode(key string) (string, error) {
	if code, found := qcodes[key]; found {
		return code, nil
	}
	if len(key) == 1 && (key[0] >= 'a' && key[0] <= 'z' || key[0] >= '0' && key[0] <= '9') {
		return key, nil
	}
	if strings.HasPrefix(key, "f") && len(key) <= 3 {
		return key, nil
	}
	return "", fmt.Errorf("no qcode for key %q", key)
}

// AbsoluteAxis scales a pixel position to QEMU's absolute pointer range.
func AbsoluteAxis(position, screenSize int) int {
	if screenSize <= 1 {
		return 0
	}
	clamped := max(0, min(position, screenSize-1))
	return clamped * absoluteAxisMax / (screenSize - 1)
}

func keyEvent(code string, down bool) map[string]any {
	return map[string]any{"type": "key", "data": map[string]any{
		"down": down, "key": map[string]any{"type": "qcode", "data": code},
	}}
}

// characterKey is the canonical key that types character on a US keyboard and whether shift is held.
func characterKey(character rune) (string, bool, error) {
	switch {
	case character >= 'a' && character <= 'z', character >= '0' && character <= '9':
		return string(character), false, nil
	case character >= 'A' && character <= 'Z':
		return string(unicode.ToLower(character)), true, nil
	case character == ' ':
		return "space", false, nil
	case character == '\n':
		return "enter", false, nil
	case character == '\t':
		return "tab", false, nil
	}
	if key, found := shiftedCharacters[character]; found {
		return key, true, nil
	}
	if _, found := qcodes[string(character)]; found {
		return string(character), false, nil
	}
	return "", false, fmt.Errorf("cannot type %q on the guest's US keyboard", character)
}

// comboCodes is the qcodes a combo presses in order; a character typed with shift on a US keyboard, such as "+",
// holds shift as well.
func comboCodes(keys []string) ([]string, error) {
	codes := make([]string, 0, len(keys)+1)
	for _, key := range keys {
		shifted := false
		if characters := []rune(key); len(characters) == 1 {
			var err error
			if key, shifted, err = characterKey(characters[0]); err != nil {
				return nil, err
			}
		}
		code, err := QCode(key)
		if err != nil {
			return nil, err
		}
		if shifted && !slices.Contains(codes, "shift") {
			codes = append(codes, "shift")
		}
		if !slices.Contains(codes, code) {
			codes = append(codes, code)
		}
	}
	return codes, nil
}

// pressEvents presses codes in order, or releases them in reverse order.
func pressEvents(codes []string, down bool) []map[string]any {
	events := make([]map[string]any, 0, len(codes))
	for index := range codes {
		code := codes[index]
		if !down {
			code = codes[len(codes)-1-index]
		}
		events = append(events, keyEvent(code, down))
	}
	return events
}

// KeyEvents presses keys in order and releases them in reverse, the way a person types a combo.
func KeyEvents(keys []string) ([]map[string]any, error) {
	codes, err := comboCodes(keys)
	if err != nil {
		return nil, err
	}
	return append(pressEvents(codes, true), pressEvents(codes, false)...), nil
}

// PointerEvents moves the absolute pointer and optionally presses or releases a button.
func PointerEvents(x, y, screenWidth, screenHeight int, button string, down bool) []map[string]any {
	events := []map[string]any{
		{"type": "abs", "data": map[string]any{"axis": "x", "value": AbsoluteAxis(x, screenWidth)}},
		{"type": "abs", "data": map[string]any{"axis": "y", "value": AbsoluteAxis(y, screenHeight)}},
	}
	if button != "" {
		events = append(events, buttonEvent(button, down))
	}
	return events
}

func buttonEvent(button string, down bool) map[string]any {
	return map[string]any{"type": "btn", "data": map[string]any{"down": down, "button": button}}
}

// The builders below return batches, each the events of one input-send-event command. QEMU syncs its input devices
// after every command and a USB tablet folds what one sync carries into one report, so every button press and
// release gets a batch of its own. The guest's keyboard queue holds few events, so keys go a combo or a character
// at a time; the caller pauses between batches so the guest drains its queues.

// KeyBatches presses a combo in one batch and releases it in the next.
func KeyBatches(keys []string) ([][]map[string]any, error) {
	codes, err := comboCodes(keys)
	if err != nil {
		return nil, err
	}
	return [][]map[string]any{pressEvents(codes, true), pressEvents(codes, false)}, nil
}

// TextBatches types text one character per batch, holding shift where the character needs it.
func TextBatches(text string) ([][]map[string]any, error) {
	var batches [][]map[string]any
	for _, character := range text {
		events, err := KeyEvents([]string{string(character)})
		if err != nil {
			return nil, err
		}
		batches = append(batches, events)
	}
	return batches, nil
}

// MoveBatches moves the pointer to x, y.
func MoveBatches(x, y, screenWidth, screenHeight int) [][]map[string]any {
	return [][]map[string]any{PointerEvents(x, y, screenWidth, screenHeight, "", false)}
}

// ClickBatches holds the modifier keys, moves to x, y, clicks count times, then lets the modifiers go.
func ClickBatches(x, y, screenWidth, screenHeight int, button string, count int, modifiers []string) (
	[][]map[string]any, error) {
	codes, err := comboCodes(modifiers)
	if err != nil {
		return nil, err
	}
	var batches [][]map[string]any
	if len(codes) > 0 {
		batches = append(batches, pressEvents(codes, true))
	}
	batches = append(batches, PointerEvents(x, y, screenWidth, screenHeight, "", false))
	for range max(count, 1) {
		batches = append(batches, []map[string]any{buttonEvent(button, true)},
			[]map[string]any{buttonEvent(button, false)})
	}
	if len(codes) > 0 {
		batches = append(batches, pressEvents(codes, false))
	}
	return batches, nil
}

// DragBatches presses the button at the first point, moves through the rest and releases it at the last.
func DragBatches(points [][2]int, screenWidth, screenHeight int, button string) [][]map[string]any {
	batches := [][]map[string]any{PointerEvents(points[0][0], points[0][1], screenWidth, screenHeight, "", false),
		{buttonEvent(button, true)}}
	for _, point := range points[1:] {
		batches = append(batches, PointerEvents(point[0], point[1], screenWidth, screenHeight, "", false))
	}
	return append(batches, []map[string]any{buttonEvent(button, false)})
}

// ScrollBatches turns the wheel at x, y one notch per batch: positive deltaY scrolls down and positive deltaX right.
func ScrollBatches(x, y, screenWidth, screenHeight, deltaX, deltaY int) [][]map[string]any {
	batches := [][]map[string]any{PointerEvents(x, y, screenWidth, screenHeight, "", false)}
	vertical, horizontal := "wheel-down", "wheel-right"
	if deltaY < 0 {
		vertical = "wheel-up"
	}
	if deltaX < 0 {
		horizontal = "wheel-left"
	}
	for range max(deltaY, -deltaY) {
		batches = append(batches, []map[string]any{buttonEvent(vertical, true), buttonEvent(vertical, false)})
	}
	for range max(deltaX, -deltaX) {
		batches = append(batches, []map[string]any{buttonEvent(horizontal, true), buttonEvent(horizontal, false)})
	}
	return batches
}
