package qemu

import (
	"fmt"
	"strings"
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

// KeyEvents presses keys in order and releases them in reverse, the way a person types a combo.
func KeyEvents(keys []string) ([]map[string]any, error) {
	codes := make([]string, 0, len(keys))
	for _, key := range keys {
		code, err := QCode(key)
		if err != nil {
			return nil, err
		}
		codes = append(codes, code)
	}
	events := make([]map[string]any, 0, 2*len(codes))
	for _, code := range codes {
		events = append(events, keyEvent(code, true))
	}
	for index := len(codes) - 1; index >= 0; index-- {
		events = append(events, keyEvent(codes[index], false))
	}
	return events, nil
}

// PointerEvents moves the absolute pointer and optionally presses or releases a button.
func PointerEvents(x, y, screenWidth, screenHeight int, button string, down bool) []map[string]any {
	events := []map[string]any{
		{"type": "abs", "data": map[string]any{"axis": "x", "value": AbsoluteAxis(x, screenWidth)}},
		{"type": "abs", "data": map[string]any{"axis": "y", "value": AbsoluteAxis(y, screenHeight)}},
	}
	if button != "" {
		events = append(events, map[string]any{"type": "btn", "data": map[string]any{"down": down, "button": button}})
	}
	return events
}
