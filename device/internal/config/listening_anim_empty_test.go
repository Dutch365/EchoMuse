package config

import (
	"encoding/json"
	"testing"
)

// home-voice-hub #171: with wake_ring_after_verify on, the controller pushes
// `listeningAnim: {}` so the device draws nothing at its own wake crossing
// and the ring waits for the controller (after the wake check). No firmware
// change rides on this: it holds only if the empty object REPLACES the cached
// spec (a nil-check, not a length check, in Apply) and then decodes to an
// empty pattern, which onWakeCrossing already refuses to draw
// (`spec.Pattern != ""` in cmd/server.go). Decoded from the controller's
// exact wire bytes, as the control client does.
func TestAnEmptyListeningAnimReplacesTheSpecAndDrawsNothing(t *testing.T) {
	d := Get()
	d.Apply(ConfigMessage{Type: "config",
		ListeningAnim: json.RawMessage(`{"pattern":"solid","colors":[[0,255,0]],"listening":true,"ttlSec":30}`)})

	var msg ConfigMessage
	if err := json.Unmarshal([]byte(`{"type":"config","listeningAnim":{}}`), &msg); err != nil {
		t.Fatalf("controller push does not decode: %v", err)
	}
	d.Apply(msg)

	got := d.Snapshot().ListeningAnim
	if len(got) == 0 {
		t.Fatal("{} must be stored as {}, so the crossing reads it and stops")
	}
	// The same decode onWakeCrossing does before it draws.
	var spec struct {
		Pattern string `json:"pattern"`
	}
	if err := json.Unmarshal(got, &spec); err != nil {
		t.Fatalf("stored spec is not valid JSON: %v", err)
	}
	if spec.Pattern != "" {
		t.Fatalf("the old spec survived the empty push: %s", got)
	}

	// And the real spec comes back on the next push (option turned off).
	d.Apply(ConfigMessage{Type: "config",
		ListeningAnim: json.RawMessage(`{"pattern":"solid"}`)})
	if err := json.Unmarshal(d.Snapshot().ListeningAnim, &spec); err != nil || spec.Pattern != "solid" {
		t.Fatalf("a real spec after {} was not taken: %s", d.Snapshot().ListeningAnim)
	}
}
