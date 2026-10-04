package qemu

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
)

func pressed(down bool) string {
	if down {
		return "down"
	}
	return "up"
}

// described renders batches as short lines such as "key shift down", "abs x 0" or "btn left up".
func described(batches [][]map[string]any) [][]string {
	rendered := [][]string{}
	for _, batch := range batches {
		lines := []string{}
		for _, event := range batch {
			data := event["data"].(map[string]any)
			switch event["type"] {
			case "key":
				lines = append(lines, fmt.Sprintf("key %s %s", data["key"].(map[string]any)["data"],
					pressed(data["down"].(bool))))
			case "btn":
				lines = append(lines, fmt.Sprintf("btn %s %s", data["button"], pressed(data["down"].(bool))))
			default:
				lines = append(lines, fmt.Sprintf("abs %s %d", data["axis"], data["value"]))
			}
		}
		rendered = append(rendered, lines)
	}
	return rendered
}

func expectBatches(t *testing.T, got [][]map[string]any, want [][]string) {
	t.Helper()
	if rendered := described(got); !reflect.DeepEqual(rendered, want) {
		t.Fatalf("batches\n%v\nwant\n%v", rendered, want)
	}
}

func TestCombosPressInOneBatchAndReleaseInReverseInTheNext(t *testing.T) {
	batches, err := KeyBatches([]string{"ctrl", "alt", "delete"})
	if err != nil {
		t.Fatal(err)
	}
	expectBatches(t, batches, [][]string{
		{"key ctrl down", "key alt down", "key delete down"},
		{"key delete up", "key alt up", "key ctrl up"},
	})
}

func TestCharactersTypedWithShiftHoldShiftOnce(t *testing.T) {
	batches, err := KeyBatches([]string{"ctrl", "+"})
	if err != nil {
		t.Fatal(err)
	}
	expectBatches(t, batches, [][]string{
		{"key ctrl down", "key shift down", "key equal down"},
		{"key equal up", "key shift up", "key ctrl up"},
	})
	batches, err = KeyBatches([]string{"shift", "?"})
	if err != nil {
		t.Fatal(err)
	}
	expectBatches(t, batches, [][]string{{"key shift down", "key slash down"}, {"key slash up", "key shift up"}})
	events, err := KeyEvents([]string{"meta", "e"})
	if err != nil || len(events) != 4 || events[0]["data"].(map[string]any)["key"].(map[string]any)["data"] != "meta_l" {
		t.Fatalf("meta combo: %v %v", events, err)
	}
}

func TestKeysWithoutAQCodeAreRefused(t *testing.T) {
	for _, keys := range [][]string{{"hyperspace"}, {"ctrl", "é"}, {"€"}} {
		if _, err := KeyBatches(keys); err == nil {
			t.Fatalf("%v must be refused", keys)
		}
	}
	if code, err := QCode("f12"); err != nil || code != "f12" {
		t.Fatalf("function keys keep their names: %q %v", code, err)
	}
}

func TestTextTypesOneCharacterPerBatch(t *testing.T) {
	batches, err := TextBatches("Hi 5!\n")
	if err != nil {
		t.Fatal(err)
	}
	expectBatches(t, batches, [][]string{
		{"key shift down", "key h down", "key h up", "key shift up"},
		{"key i down", "key i up"},
		{"key spc down", "key spc up"},
		{"key 5 down", "key 5 up"},
		{"key shift down", "key 1 down", "key 1 up", "key shift up"},
		{"key ret down", "key ret up"},
	})
}

func TestEveryPrintableUSCharacterTypes(t *testing.T) {
	var printable strings.Builder
	for character := ' '; character <= '~'; character++ {
		printable.WriteRune(character)
	}
	batches, err := TextBatches(printable.String() + "\t")
	if err != nil {
		t.Fatal(err)
	}
	if len(batches) != len(printable.String())+1 {
		t.Fatalf("%d batches for %d characters", len(batches), len(printable.String())+1)
	}
	for index, batch := range batches {
		if len(batch) != 2 && len(batch) != 4 {
			t.Fatalf("character %d: %v", index, described([][]map[string]any{batch}))
		}
	}
	if _, err := TextBatches("café"); err == nil || !strings.Contains(err.Error(), "é") {
		t.Fatalf("characters off the US keyboard: %v", err)
	}
}

func TestClicksHoldModifiersAndSendEachButtonChangeAlone(t *testing.T) {
	batches, err := ClickBatches(640, 400, 1281, 801, "right", 2, []string{"ctrl", "shift"})
	if err != nil {
		t.Fatal(err)
	}
	expectBatches(t, batches, [][]string{
		{"key ctrl down", "key shift down"},
		{"abs x 16383", "abs y 16383"},
		{"btn right down"}, {"btn right up"}, {"btn right down"}, {"btn right up"},
		{"key shift up", "key ctrl up"},
	})
	batches, err = ClickBatches(0, 0, 100, 100, "left", 0, nil)
	if err != nil {
		t.Fatal(err)
	}
	expectBatches(t, batches, [][]string{{"abs x 0", "abs y 0"}, {"btn left down"}, {"btn left up"}})
	if _, err := ClickBatches(0, 0, 100, 100, "left", 1, []string{"hyper"}); err == nil {
		t.Fatal("unknown modifiers must be refused")
	}
}

func TestDragsHoldTheButtonAlongThePath(t *testing.T) {
	batches := DragBatches([][2]int{{0, 0}, {50, 50}, {99, 99}}, 100, 100, "left")
	expectBatches(t, batches, [][]string{
		{"abs x 0", "abs y 0"}, {"btn left down"},
		{"abs x 16548", "abs y 16548"}, {"abs x 32767", "abs y 32767"},
		{"btn left up"},
	})
	if moves := MoveBatches(99, 0, 100, 100); !reflect.DeepEqual(described(moves),
		[][]string{{"abs x 32767", "abs y 0"}}) {
		t.Fatalf("move: %v", described(moves))
	}
}

func notch(button string) []string {
	return []string{"btn " + button + " down", "btn " + button + " up"}
}

func TestScrollsTurnTheWheelOneNotchPerBatch(t *testing.T) {
	batches := ScrollBatches(10, 20, 100, 100, -2, 3)
	want := [][]string{{"abs x 3309", "abs y 6619"}}
	for range 3 {
		want = append(want, notch("wheel-down"))
	}
	for range 2 {
		want = append(want, notch("wheel-left"))
	}
	expectBatches(t, batches, want)
	expectBatches(t, ScrollBatches(0, 0, 100, 100, 1, -1), [][]string{{"abs x 0", "abs y 0"},
		notch("wheel-up"), notch("wheel-right")})
}
