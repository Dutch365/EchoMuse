package client

import (
	"sync"
	"time"

	"github.com/wilbowes/EchoMuse/internal/listen"
)

// The audio an on-device wake word was scored against, kept on the device
// until that wake is sent. Private listening still sends nothing before the
// wake: this ring is memory only, and the bytes leave in the oww_wake message
// itself, once, when the controller has announced FeatureWakePreroll.
//
// The clip is the ~1.5s ending at the crossing frame. "Hey Leo" is about
// 0.6–0.8s, and the session that follows the wake starts after the crossing,
// so the controller otherwise hears none of the word. The scorer reports the
// crossing up to one queue later (8 frames, 640ms), so the ring is the clip
// plus that slack: a callback that late still has the whole 1.5s.

const (
	// PrerollSendFrames is 19 × 80ms = 1.52s of 16 kHz mono S16_LE.
	PrerollSendFrames = 19
	// PrerollSlackFrames is the scorer queue (shadow.Scorer holds 8).
	PrerollSlackFrames = 8
	prerollKeepFrames  = PrerollSendFrames + PrerollSlackFrames
	// PrerollMaxBytes is the most a wake message will carry. 2s, above the
	// 1.52s clip, and the same ceiling the controller refuses past.
	PrerollMaxBytes = 2 * 16000 * 2
)

type prerollFrame struct {
	at  time.Time
	pcm []byte
}

// prerollRing is the wake stream's recent frames. push runs on the mic
// goroutine; snapshot runs on the scorer goroutine when a wake fires.
type prerollRing struct {
	mu     sync.Mutex
	frames [prerollKeepFrames]prerollFrame
	n      int
}

func (r *prerollRing) push(at time.Time, pcm []byte) {
	if len(pcm) == 0 {
		return
	}
	// Own the bytes. The mic loop hands the same slice to the listen gate,
	// and that gate drops its reference when a session opens.
	cp := make([]byte, len(pcm))
	copy(cp, pcm)
	r.mu.Lock()
	r.frames[r.n%prerollKeepFrames] = prerollFrame{at, cp}
	r.n++
	r.mu.Unlock()
}

// snapshot is the clip ending at the crossing frame, or nil when that frame
// has already left the ring. Frames captured after it are the command, not
// the wake word, and are not included.
func (r *prerollRing) snapshot(at time.Time) []byte {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.n == 0 {
		return nil
	}
	oldest := r.n - prerollKeepFrames
	if oldest < 0 {
		oldest = 0
	}
	end := -1
	for i := r.n - 1; i >= oldest; i-- {
		if !r.frames[i%prerollKeepFrames].at.After(at) {
			end = i
			break
		}
	}
	if end < 0 || !r.frames[end%prerollKeepFrames].at.Equal(at) {
		return nil
	}
	start := end - PrerollSendFrames + 1
	if start < oldest {
		start = oldest
	}
	var out []byte
	for i := start; i <= end; i++ {
		out = append(out, r.frames[i%prerollKeepFrames].pcm...)
		if len(out) > PrerollMaxBytes {
			out = out[len(out)-PrerollMaxBytes:]
		}
	}
	if len(out)%2 != 0 {
		out = out[1:]
	}
	return out
}

// WakePreroll is the clip for a wake whose crossing frame was captured at.
// Nil when that frame is no longer buffered. listen.FrameBytes is the size
// of one wake-stream frame; the ring does not assume it, the mic loop does.
func (d *DataClient) WakePreroll(at time.Time) []byte {
	return d.wakePreroll.snapshot(at)
}

// prerollFrameBytes is one wake-stream frame. Kept next to the ring so a
// test can show the resident size without importing the mic loop.
func prerollResidentBytes() int {
	return prerollKeepFrames * listen.FrameBytes
}
