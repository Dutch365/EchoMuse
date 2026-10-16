"""
The speech gate: a turn's audio reaches HA only once a frame scores as speech,
and when it does, HA receives the whole held stream, in order.

Silero itself needs onnxruntime, which the suite does not have, so the model
is stubbed; its per-turn state handling is tested against a stub session.
"""

import logging

import numpy as np

import em_speechgate as sg
import em_wakeverify


def _frames(n):
    return [bytes([i]) * 2560 for i in range(n)]


def test_holds_everything_below_threshold():
    g = sg.SpeechGate(threshold=0.5)
    for f in _frames(20):
        assert g.push(f, 0.03) == []
    assert not g.open
    assert g.peak == 0.03


def test_opening_releases_the_held_stream_in_order_then_passes_through():
    g = sg.SpeechGate(threshold=0.5)
    fs = _frames(6)
    for f in fs[:3]:
        g.push(f, 0.1)
    # The opening frame comes out LAST, after what was held before it: HA must
    # receive exactly the ungated stream, only later.
    assert g.push(fs[3], 0.9) == fs[:4]
    assert g.open and g.opened_at == 3
    # Once open, nothing is scored against the bar: speech has pauses.
    assert g.push(fs[4], 0.0) == [fs[4]]
    assert g.push(fs[5], 0.0) == [fs[5]]


def test_threshold_is_inclusive():
    g = sg.SpeechGate(threshold=0.5)
    assert g.push(b"x", 0.5) == [b"x"]


def test_hold_is_bounded_and_keeps_the_newest():
    g = sg.SpeechGate(threshold=0.5, max_hold_s=0.4)   # 5 frames
    fs = _frames(8)
    for f in fs[:7]:
        g.push(f, 0.0)
    assert g.dropped == 2
    assert g.push(fs[7], 1.0) == fs[3:8]


def test_summary_says_nothing_was_sent_when_it_never_opened():
    g = sg.SpeechGate()
    for f in _frames(3):
        g.push(f, 0.02)
    assert "nothing sent" in g.summary()
    assert "peak 0.02" in g.summary()


class _Session:
    """Stub ONNX session: probability = mean |x| of the chunk; counts state."""

    def __init__(self):
        self.calls = []

    def run(self, _outs, feeds):
        x = feeds["input"]
        assert x.shape == (1, sg.CHUNK)
        self.calls.append(float(feeds["h"][0, 0, 0]))
        h = feeds["h"] + 1.0     # state must be carried from call to call
        return [np.array([[float(np.abs(x).mean())]]), h, feeds["c"]]


def test_the_default_threshold_is_unchanged():
    g = sg.SpeechGate()
    assert g.threshold == 0.5
    assert g.near_miss is False
    assert g.push(b"a", 0.49) == []
    assert g.push(b"b", 0.5) == [b"a", b"b"]
    assert g.near_miss_sent is False


def test_a_near_miss_under_the_threshold_is_sent_when_allowed(caplog):
    caplog.set_level(logging.INFO, logger="echomuse")
    g = sg.SpeechGate(threshold=0.5, near_miss=True)
    assert g.push(b"held", 0.1) == []
    assert g.push(b"peak", 0.49) == [b"held", b"peak"]
    assert g.open and g.near_miss_sent
    assert "gate near-miss sent 0.49" in caplog.text
    # Once open, later frames pass, and the line is not repeated.
    assert g.push(b"next", 0.1) == [b"next"]
    assert caplog.text.count("gate near-miss sent") == 1


def test_a_verifier_reject_still_drops_audio_under_the_threshold(caplog):
    caplog.set_level(logging.INFO, logger="echomuse")
    g = sg.SpeechGate(threshold=0.5, near_miss=False)
    assert g.push(b"a", 0.47) == []
    assert not g.open
    assert "gate near-miss sent" not in caplog.text


def test_the_near_miss_floor_is_a_quarter_and_the_threshold_still_opens(caplog):
    caplog.set_level(logging.INFO, logger="echomuse")
    low = sg.SpeechGate(near_miss=True)
    assert low.push(b"a", 0.24) == []
    assert not low.open
    opened = sg.SpeechGate(near_miss=True)
    assert opened.push(b"b", 0.25) == [b"b"]
    assert "gate near-miss sent 0.25" in caplog.text
    normal = sg.SpeechGate(near_miss=True)
    caplog.clear()
    assert normal.push(b"c", 0.5) == [b"c"]
    assert normal.near_miss_sent is False
    assert "gate near-miss sent" not in caplog.text


def test_threshold_clamps_and_the_env_overlays_only_when_set():
    assert sg.clamp_threshold(0.33) == 0.33
    assert sg.clamp_threshold(0.1) == 0.2
    assert sg.clamp_threshold(0.9) == 0.8
    assert sg.clamp_threshold("nope") == 0.5
    assert sg.clamp_threshold(float("nan")) == 0.5
    stored = {"speechGateThreshold": 0.4}
    assert sg.overlay(stored, {}) is stored
    assert sg.overlay(stored, {"EM_SPEECH_GATE_THRESHOLD": ""}) is stored
    over = sg.overlay(stored, {"EM_SPEECH_GATE_THRESHOLD": "0.7"})
    assert over["speechGateThreshold"] == 0.7
    assert stored["speechGateThreshold"] == 0.4
    bad = sg.overlay(stored, {"EM_SPEECH_GATE_THRESHOLD": "banana"})
    assert bad["speechGateThreshold"] == 0.5

    class Dev:
        pass
    dev = Dev()
    sg.apply(dev, {"speechGateThreshold": 0.33})
    assert dev.speech_gate_threshold == 0.33
    sg.apply(dev, None)
    assert dev.speech_gate_threshold == 0.5
    sg.apply(dev, {"speechGateThreshold": 9})
    assert dev.speech_gate_threshold == 0.8
    assert sg.threshold_for(dev) == 0.8


def test_gate_for_follows_the_explicit_accept(monkeypatch):
    class Dev:
        device_id = "G090"
        speech_gate_threshold = 0.6

    monkeypatch.setattr(em_wakeverify, "consume_accept", lambda device_id: True)
    gate = sg.gate_for(Dev(), vad=object())
    assert gate.threshold == 0.6 and gate.near_miss is True
    assert gate.push(b"a", 0.4) == [b"a"]

    monkeypatch.setattr(em_wakeverify, "consume_accept", lambda device_id: False)
    quiet = sg.gate_for(Dev(), vad=object())
    assert quiet.near_miss is False
    assert quiet.push(b"a", 0.4) == []

    seen = []
    monkeypatch.setattr(
        em_wakeverify, "consume_accept", lambda device_id: seen.append(device_id) or True)
    assert sg.gate_for(Dev(), None) is None
    assert seen == ["G090"]


def test_silero_scores_two_chunks_per_frame_and_carries_state():
    sess = _Session()
    v = sg.Silero(sess)
    loud = (np.ones(1280) * 16384).astype(np.int16).tobytes()
    p = v.prob(loud)
    assert abs(p - 0.5) < 1e-3
    v.prob(loud)
    assert sess.calls == [0.0, 1.0, 2.0, 3.0]


def test_a_new_turn_starts_from_clean_state():
    sess = _Session()
    sg.Silero(sess).prob(bytes(2560))
    sess.calls.clear()
    sg.Silero(sess).prob(bytes(2560))
    assert sess.calls[0] == 0.0
