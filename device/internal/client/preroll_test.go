package client

import (
	"bytes"
	"encoding/json"
	"testing"
	"time"
)

func prerollAt(i int) time.Time {
	return time.Unix(0, 0).Add(time.Duration(i) * 80 * time.Millisecond)
}

func fillPreroll(r *prerollRing, from, n int, tag byte) time.Time {
	var at time.Time
	for i := 0; i < n; i++ {
		at = prerollAt(from + i)
		pcm := bytes.Repeat([]byte{tag, byte(from + i)}, 4)
		r.push(at, pcm)
	}
	return at
}

func TestPrerollSnapshotEndsAtTheCrossing(t *testing.T) {
	var r prerollRing
	// More than a clip of audio before the crossing, then frames after it
	// that the scorer has not reported yet.
	at := fillPreroll(&r, 0, 30, 0x11)
	r.push(at.Add(80*time.Millisecond), []byte{0xEE, 0xEE})
	r.push(at.Add(160*time.Millisecond), []byte{0xEE, 0xEE})

	got := r.snapshot(at)
	if got == nil {
		t.Fatal("crossing frame was in the ring")
	}
	if bytes.Contains(got, []byte{0xEE, 0xEE}) {
		t.Fatal("snapshot included audio captured after the crossing")
	}
	// 19 frames, each 8 bytes, ending with the crossing's tag.
	if len(got) != PrerollSendFrames*8 {
		t.Fatalf("clip length %d, want %d", len(got), PrerollSendFrames*8)
	}
	if got[len(got)-2] != 0x11 || got[len(got)-1] != byte(29) {
		t.Fatalf("clip does not end on the crossing frame: %x", got[len(got)-2:])
	}
	// The oldest frame in the clip is index 11 (29-18), not frame 0.
	if got[1] != 11 {
		t.Fatalf("clip starts at frame %d, want 11", got[1])
	}
}

func TestPrerollSurvivesTheScorerQueue(t *testing.T) {
	var r prerollRing
	at := fillPreroll(&r, 0, PrerollSendFrames, 0x22)
	// The scorer queue is 8 frames. Those arrive before the callback and
	// must not evict the start of the clip.
	for i := 0; i < PrerollSlackFrames; i++ {
		r.push(at.Add(time.Duration(i+1)*80*time.Millisecond), []byte{0xFF, 0xFF})
	}
	got := r.snapshot(at)
	if len(got) != PrerollSendFrames*8 {
		t.Fatalf("after %d late frames, clip is %d bytes, want %d",
			PrerollSlackFrames, len(got), PrerollSendFrames*8)
	}
	if bytes.Contains(got, []byte{0xFF, 0xFF}) {
		t.Fatal("late frames leaked into the clip")
	}
}

func TestPrerollDropsTheCrossingOnceTheSlackIsExceeded(t *testing.T) {
	var r prerollRing
	at := fillPreroll(&r, 0, 1, 0x33)
	// One frame in the ring, then more than the ring can hold after it.
	for i := 0; i < prerollKeepFrames; i++ {
		r.push(at.Add(time.Duration(i+1)*80*time.Millisecond), []byte{0x01, 0x02})
	}
	if got := r.snapshot(at); got != nil {
		t.Fatalf("crossing frame should have left the ring, got %d bytes", len(got))
	}
}

func TestPrerollShortRingIsWhatThereIs(t *testing.T) {
	var r prerollRing
	at := fillPreroll(&r, 0, 3, 0x44)
	got := r.snapshot(at)
	if len(got) != 3*8 {
		t.Fatalf("short clip %d bytes, want %d", len(got), 24)
	}
}

func TestPrerollMissAndEmpty(t *testing.T) {
	var r prerollRing
	if r.snapshot(prerollAt(0)) != nil {
		t.Fatal("empty ring returned a clip")
	}
	at := fillPreroll(&r, 0, 4, 0x55)
	if r.snapshot(at.Add(time.Millisecond)) != nil {
		t.Fatal("a time that is not a frame must not invent a clip")
	}
}

func TestPrerollPushCopies(t *testing.T) {
	var r prerollRing
	pcm := []byte{0x01, 0x02, 0x03, 0x04}
	at := prerollAt(1)
	r.push(at, pcm)
	pcm[0] = 0xFF
	got := r.snapshot(at)
	if got[0] != 0x01 {
		t.Fatalf("ring kept a live buffer: %x", got)
	}
}

func TestPrerollResidentSize(t *testing.T) {
	// 27 frames × 2560 bytes. The listen gate already holds its own 2s ring
	// while a session is closed; this is the additional resident audio.
	const want = 27 * 2560
	if got := prerollResidentBytes(); got != want {
		t.Fatalf("resident %d, want %d", got, want)
	}
	if PrerollSendFrames*2560 > PrerollMaxBytes {
		t.Fatal("the clip the device sends is larger than the controller accepts")
	}
}

func TestOwwWakeCarriesPrerollOnlyWhenPresent(t *testing.T) {
	at := time.Unix(1_700_000_000, 0)
	plain := owwWakeMessage(0.9, 0.5, at, 7, 0.01, false, nil, nil)
	if _, ok := plain["preRoll"]; ok {
		t.Fatal("a wake with no clip must omit preRoll — older controllers ignore unknown fields, and the field is large")
	}
	if plain["session"] != uint32(7) {
		t.Fatalf("session = %v", plain["session"])
	}

	clip := bytes.Repeat([]byte{0x00, 0x10}, 100)
	with := owwWakeMessage(0.9, 0.5, at, 7, 0.01, true, nil, clip)
	raw, err := json.Marshal(with)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	s, ok := decoded["preRoll"].(string)
	if !ok || s == "" {
		t.Fatalf("preRoll missing in %s", raw)
	}
	// encoding/json base64-encodes []byte. The controller decodes that.
	if decoded["session"].(float64) != 7 || decoded["barge"] != true {
		t.Fatalf("wake fields changed: %s", raw)
	}

	// No session: a streaming on-device wake still carries the clip, and
	// still omits the private-listening fields.
	stream := owwWakeMessage(0.4, 0.5, at, 0, 0, false, nil, clip)
	if _, ok := stream["session"]; ok {
		t.Fatal("session 0 must stay omitted")
	}
	if _, ok := stream["preRoll"]; !ok {
		t.Fatal("streaming on-device wake dropped the clip")
	}
}
