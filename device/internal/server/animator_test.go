package server

import (
	"math"
	"testing"
)

func TestSpinFrame(t *testing.T) {
	spec := AnimSpec{
		Pattern: "spin",
		Colors:  [][3]uint8{{0, 200, 0}, {0, 60, 0}},
	}
	frame := animFrame(spec, 3)
	if frame[3].G != 200 {
		t.Fatalf("head not at pos 3: %+v", frame[3])
	}
	if frame[2].G != 60 {
		t.Fatalf("trail not at pos 2: %+v", frame[2])
	}
	for i, l := range frame {
		if i == 3 || i == 2 {
			continue
		}
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("LED %d not dark: %+v", i, l)
		}
		if l.ID != i {
			t.Fatalf("LED %d has wrong ID %d", i, l.ID)
		}
	}
	// Wraparound: head at 0 puts trail at 11.
	frame = animFrame(spec, 0)
	if frame[0].G != 200 || frame[11].G != 60 {
		t.Fatalf("wraparound wrong: head=%+v trail=%+v", frame[0], frame[11])
	}
}

func TestRotateFrame(t *testing.T) {
	palette := make([][3]uint8, 12)
	for i := range palette {
		palette[i] = [3]uint8{uint8(i), 0, 0}
	}
	spec := AnimSpec{Pattern: "rotate", Colors: palette}
	// pos=0 is the palette 1:1; pos=1 shifts every colour one LED clockwise.
	frame := animFrame(spec, 0)
	for i := range frame {
		if frame[i].R != uint8(i) {
			t.Fatalf("pos 0: LED %d = %d, want %d", i, frame[i].R, i)
		}
	}
	frame = animFrame(spec, 1)
	if frame[1].R != 0 || frame[0].R != 11 {
		t.Fatalf("pos 1 rotation wrong: led0=%d led1=%d", frame[0].R, frame[1].R)
	}
}

func TestRotateFrameEmptyPalette(t *testing.T) {
	frame := animFrame(AnimSpec{Pattern: "rotate"}, 5)
	for i, l := range frame {
		if l.R != 0 || l.G != 0 || l.B != 0 {
			t.Fatalf("LED %d not dark on empty palette: %+v", i, l)
		}
	}
}

func TestPaletteFrame(t *testing.T) {
	// Single colour fills the ring.
	frame := paletteFrame([][3]uint8{{10, 20, 30}})
	for i, l := range frame {
		if l.R != 10 || l.G != 20 || l.B != 30 {
			t.Fatalf("LED %d wrong: %+v", i, l)
		}
	}
	// Short multi-colour list leaves the rest dark.
	frame = paletteFrame([][3]uint8{{1, 0, 0}, {2, 0, 0}})
	if frame[0].R != 1 || frame[1].R != 2 || frame[2].R != 0 {
		t.Fatalf("partial palette wrong: %+v", frame[:3])
	}
}

func TestResolveMeterDefaultsAndClamps(t *testing.T) {
	// Absent fields fall back to the shipped curve.
	a, d, f, g, r, c := resolveMeter(AnimSpec{Pattern: "meter"})
	if a != meterDefaults.attack || d != meterDefaults.decay ||
		f != meterDefaults.floor || g != meterDefaults.gamma ||
		r != meterDefaults.ref || c != meterDefaults.curve {
		t.Fatalf("defaults not applied: %v %v %v %v %v %v", a, d, f, g, r, c)
	}

	// floor=0 must survive: it is a legitimate value (fully dark at
	// silence), which is the whole reason these fields are pointers.
	zero := 0.0
	_, _, f0, _, _, _ := resolveMeter(AnimSpec{Floor: &zero})
	if f0 != 0.0 {
		t.Fatalf("floor 0 not honoured, got %v", f0)
	}

	// Out-of-range values clamp rather than producing a dead ring: a
	// config push must not be able to break the display.
	huge, neg := 99.0, -5.0
	at, dc, fl, gm, rf, cv := resolveMeter(AnimSpec{
		Attack: &huge, Decay: &neg, Floor: &huge,
		Gamma: &neg, Ref: &neg, Curve: &huge,
	})
	if at != 1.0 || dc != 0.02 || fl != 0.6 || gm != 1.0 || rf != 0.02 || cv != 2.0 {
		t.Fatalf("clamping wrong: %v %v %v %v %v %v", at, dc, fl, gm, rf, cv)
	}
}

// TestMeterCurveIsVisiblyVaried is the regression guard for the reported
// "too subtle to distinguish from a solid ring" bug. It asserts the shipped
// curve produces a wide PERCEPTUAL swing across ordinary speech levels —
// the old curve (sqrt(rms/0.35), floor .15, no gamma) managed only ~23%.
func TestMeterCurveIsVisiblyVaried(t *testing.T) {
	_, _, floor, gamma, ref, curve := resolveMeter(AnimSpec{Pattern: "meter"})
	span := 1.0 - floor

	// Perceived lightness of a painted duty cycle b is ~b^(1/gamma), so
	// with the gamma encoding the perceptual value is just floor+span*env.
	perceived := func(rms float64) float64 {
		env := math.Pow(math.Min(1, rms/ref), curve)
		b := math.Pow(floor+span*env, gamma)
		return math.Pow(b, 1.0/gamma)
	}

	quiet, loud := perceived(0.02), perceived(0.20)
	if got := loud - quiet; got < 0.55 {
		t.Fatalf("perceptual swing across speech only %.2f, want >=0.55 "+
			"(quiet=%.2f loud=%.2f) — meter will read as a solid ring", got, quiet, loud)
	}
	// Silence must still be visibly lit: the ring belongs to the turn.
	if s := perceived(0.0); s < 0.03 {
		t.Fatalf("silence too dark (%.3f) — ring reads as off mid-response", s)
	}
	// And full scale must not clip below full brightness.
	if p := perceived(1.0); p < 0.99 {
		t.Fatalf("peak not full brightness: %.3f", p)
	}
}

func TestCountdownLitCount(t *testing.T) {
	cases := []struct {
		total, remain, want int
	}{
		{0, 0, 0},
		{60, 0, 0},
		{60, 60, 12},
		{60, 30, 6},
		{60, 1, 1},   // tip stays lit while any time remains
		{120, 12, 1}, // round(12/120*12)=1
		{100, 50, 6},
		{10, 9, 11}, // round(0.9*12)=11
	}
	for _, c := range cases {
		got := countdownLitCount(c.total, c.remain)
		if got != c.want {
			t.Fatalf("countdownLitCount(%d,%d)=%d want %d", c.total, c.remain, got, c.want)
		}
	}
}

func TestCountdownBlinkThreshold(t *testing.T) {
	// 10% of 600s = 60 > floor 15 → 60
	if th := countdownBlinkThreshold(600, 0.10, 15); th != 60 {
		t.Fatalf("600s threshold=%d want 60", th)
	}
	// 10% of 60s = 6 < floor 15 → 15
	if th := countdownBlinkThreshold(60, 0.10, 15); th != 15 {
		t.Fatalf("60s threshold=%d want 15", th)
	}
	// tiny timer: floor capped at total
	if th := countdownBlinkThreshold(10, 0.10, 15); th != 10 {
		t.Fatalf("10s threshold=%d want 10", th)
	}
}

func TestCountdownFrameTipBlink(t *testing.T) {
	colour := [3]uint8{255, 214, 170}
	on := countdownFrame(3, colour, false)
	if on[0].R != 255 || on[1].R != 255 || on[2].R != 255 {
		t.Fatalf("lit LEDs wrong: %+v", on[:3])
	}
	if on[3].R != 0 {
		t.Fatalf("LED 3 should be dark")
	}
	off := countdownFrame(3, colour, true)
	if off[0].R != 255 || off[1].R != 255 {
		t.Fatalf("base lit wrong during blink-off")
	}
	if off[2].R != 0 || off[2].G != 0 || off[2].B != 0 {
		t.Fatalf("tip should be dark in blink-off phase: %+v", off[2])
	}
}
