package sendspin

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/flynn/noise"
	"github.com/gorilla/websocket"
)

// A session wired to a loopback WebSocket with a fixed-key transport, so
// what onTime sends can be read back as the server would see it. No
// handshake and no network beyond 127.0.0.1.
func loopbackSession(t *testing.T) (*session, <-chan envelope) {
	t.Helper()
	var key [32]byte
	key[0] = 1
	got := make(chan envelope, 16)
	up := websocket.Upgrader{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		recv := noise.UnsafeNewCipherState(cipherSuite, key, 0)
		for {
			_, ct, err := conn.ReadMessage()
			if err != nil {
				return
			}
			pt, err := recv.Decrypt(nil, nil, ct)
			if err != nil || len(pt) == 0 || pt[0] != msgJSON {
				t.Errorf("undecodable frame: %v", err)
				return
			}
			var env envelope
			if err := json.Unmarshal(pt[1:], &env); err != nil {
				t.Errorf("bad json: %v", err)
				return
			}
			got <- env
		}
	}))
	t.Cleanup(srv.Close)

	ws, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ws.Close() })
	c, err := New(Config{StorePath: filepath.Join(t.TempDir(), "s.json")})
	if err != nil {
		t.Fatal(err)
	}
	s := newSession(c, ws)
	s.tr = &transport{send: noise.UnsafeNewCipherState(cipherSuite, key, 0)}
	s.roles = []string{rolePlayer}
	return s, got
}

func next(t *testing.T, got <-chan envelope) envelope {
	t.Helper()
	select {
	case env := <-got:
		return env
	case <-time.After(2 * time.Second):
		t.Fatal("nothing sent")
	}
	return envelope{}
}

// A reply the server stamped 1ms after the client asked, with no offset.
func replyTo(sent int64) serverTime {
	return serverTime{ClientTransmitted: sent, ServerReceived: sent + 500, ServerTransmitted: sent + 600}
}

// Partial backport of upstream 7ba591a (#1159): while the filter has not
// converged, onTime asks again at once instead of waiting for timeSync's
// 200ms ladder; once converged, it sends the client/state that was held.
func TestUnconvergedOnTimeAsksAgainAndConvergedSendsTheHeldState(t *testing.T) {
	s, got := loopbackSession(t)
	s.sendState() // activation: held, clock not converged
	select {
	case env := <-got:
		t.Fatalf("state sent before the clock converged: %s", env.Type)
	case <-time.After(50 * time.Millisecond):
	}

	t0 := nowUs() - 1000
	s.onTime(replyTo(t0))
	if s.filter.synchronized() {
		t.Fatal("one measurement should not converge the filter")
	}
	env := next(t, got)
	if env.Type != "client/time" {
		t.Fatalf("unconverged onTime sent %q, want client/time", env.Type)
	}
	var ct clientTime
	if err := json.Unmarshal(env.Payload, &ct); err != nil || ct.ClientTransmitted <= t0 {
		t.Fatalf("client/time carries no fresh timestamp: %+v %v", ct, err)
	}

	time.Sleep(2 * time.Millisecond)
	s.onTime(replyTo(ct.ClientTransmitted))
	if !s.filter.synchronized() {
		t.Fatal("two measurements should converge the filter")
	}
	env = next(t, got)
	if env.Type != "client/state" {
		t.Fatalf("converged onTime sent %q, want the held client/state", env.Type)
	}
	s.mu.Lock()
	due, sent := s.stateDue, s.stateSent
	s.mu.Unlock()
	if due || !sent {
		t.Errorf("stateDue=%v stateSent=%v after the held state went out", due, sent)
	}

	// Converged and nothing held: a further reply sends nothing.
	time.Sleep(2 * time.Millisecond)
	s.onTime(replyTo(nowUs() - 1000))
	select {
	case env := <-got:
		t.Fatalf("converged onTime with nothing due sent %q", env.Type)
	case <-time.After(50 * time.Millisecond):
	}
}
