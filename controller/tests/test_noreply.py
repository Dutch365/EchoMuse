"""A failed turn carries a reason. The miss tone plays for a real request."""

import asyncio
import logging
import os
import time
from pathlib import Path

import pytest

import em_db
import em_noreply as nr


@pytest.fixture(autouse=True)
def _reset_address_hook():
    nr.address_hook = None
    yield
    nr.address_hook = None


@pytest.mark.parametrize("outcome,text,code", [
    ("ok", "hello", ""),
    ("answered", "", ""),
    ("cancelled", "x", ""),
    ("barged", "x", ""),
    ("superseded", "", ""),
    ("", "", ""),
    ("no_tts", "", "no_stt"),
    ("no_tts", "   ", "no_stt"),
    ("no_tts", "turn the light on", "no_tts"),
    ("timeout", "", "timeout"),
    ("tts_error", "hi", "tts_error"),
    ("no_speech", "", "no_speech"),
    ("stream_timeout", "", "stream_timeout"),
    ("pipeline_refused", "", "pipeline_refused"),
    ("no_ha", "", "no_ha"),
    ("something_new", "", "something_new"),
])
def test_every_failure_has_a_code_and_a_success_does_not(outcome, text, code):
    assert nr.reason_code(outcome, text) == code


def test_the_miss_tone_needs_a_real_request_and_the_timeout_does_not(monkeypatch):
    monkeypatch.delenv("EM_NO_REPLY_CUE", raising=False)
    monkeypatch.delenv("EM_REPLY_FALLBACK", raising=False)
    monkeypatch.delenv("EM_CUE_WAKE_SCORE", raising=False)
    # No score and no transcript: silence is the default.
    assert nr.cue_kind("no_stt") == ""
    assert nr.cue_kind("no_tts") == ""
    assert nr.cue_kind("no_speech") == ""
    assert nr.cue_kind("tts_error") == ""
    assert nr.cue_kind("timeout") == "fallback"
    assert nr.cue_kind("no_stt", {"wake_score": 0.9}) == "miss"
    assert nr.cue_kind("no_tts", {"wake_score": 0.9}) == "miss"
    monkeypatch.setenv("EM_NO_REPLY_CUE", "0")
    monkeypatch.setenv("EM_REPLY_FALLBACK", "0")
    assert nr.cue_kind("no_stt", {"wake_score": 0.99}) == ""
    assert nr.cue_kind("timeout") == ""
    assert nr.enabled("EM_NO_REPLY_CUE") is False


def test_zero_is_off_and_the_words_for_on_are_on():
    assert nr.enabled("EM_DOES_NOT_EXIST", True) is True
    os.environ["EM_NO_REPLY_CUE"] = "false"
    try:
        assert nr.enabled("EM_NO_REPLY_CUE") is False
    finally:
        del os.environ["EM_NO_REPLY_CUE"]


def test_a_high_wake_counts_and_a_low_or_missing_one_does_not(monkeypatch):
    monkeypatch.delenv("EM_CUE_WAKE_SCORE", raising=False)
    assert nr.real_request({"wake_score": 0.85}) is True
    assert nr.real_request({"wake_score": 0.849}) is False
    assert nr.real_request({"wake_score": 0.5, "stt_text": ""}) is False
    assert nr.real_request({}) is False
    assert nr.real_request({"wake_score": None}) is False
    assert nr.real_request({"wake_score": "0.99"}) is False
    assert nr.real_request({"wake_score": True}) is False
    assert nr.real_request({"wake_score": float("nan")}) is False
    assert nr.real_request({"wake_score": float("inf")}) is False
    assert nr.real_request("not a turn") is False
    monkeypatch.setenv("EM_CUE_WAKE_SCORE", "0.6")
    assert nr.real_request({"wake_score": 0.6}) is True
    assert nr.real_request({"wake_score": 0.59}) is False
    monkeypatch.setenv("EM_CUE_WAKE_SCORE", " ")
    assert nr.real_request({"wake_score": 0.85}) is True
    monkeypatch.setenv("EM_CUE_WAKE_SCORE", "nope")
    assert nr.real_request({"wake_score": 0.85}) is True
    assert nr.real_request({"wake_score": 0.5}) is False
    monkeypatch.setenv("EM_CUE_WAKE_SCORE", "1.4")
    assert nr.real_request({"wake_score": 1.0}) is True
    assert nr.real_request({"wake_score": 0.99}) is False


def test_chatter_and_television_stay_silent_and_a_directed_transcript_does_not(monkeypatch):
    monkeypatch.delenv("EM_CUE_ADDRESS", raising=False)
    monkeypatch.delenv("EM_CUE_MIN_WORDS", raising=False)
    monkeypatch.delenv("EM_CUE_MIN_CHARS", raising=False)
    tv = "and now we return to the programme after the break"
    assert nr.real_request({
        "stt_text": tv, "wake_score": 0.42, "trigger": "wakeword(0.420)",
        "wake_model": "hey_jarvis_v0.1",
    }) is False
    # The name is in the transcript, so the low score does not matter.
    assert nr.real_request({
        "stt_text": "hey jarvis, turn the lights off",
        "wake_score": 0.42,
        "wake_model": "hey_jarvis_v0.1",
    }) is True
    # A custom model is read from its filename, the same way the dashboard names it.
    assert nr.real_request({
        "stt_text": "ok nabu what time is it",
        "wake_model": "/data/oww_models/ok_nabu.onnx",
        "wake_score": 0.1,
    }) is True
    # The name as a fragment of another word is not an address.
    assert nr.real_request({
        "stt_text": "the jarvison report continues tonight",
        "wake_model": "jarvis_v0.1",
    }) is False
    assert nr.real_request({
        "stt_text": "turn the lights off", "trigger": "button(1)",
    }) is True
    assert nr.real_request({
        "stt_text": "turn the lights off", "trigger": "buttons",
    }) is False
    assert nr.real_request({
        "stt_text": "hey jarvis turn the lights off",
        "wake_model": 12,
    }) is False
    # A button press with a real command is directed. A mutter is not.
    assert nr.real_request({
        "stt_text": "turn the lights off", "trigger": "button", "wake_score": None,
    }) is True
    assert nr.real_request({
        "stt_text": "uh", "trigger": "button",
    }) is False
    assert nr.real_request({"stt_text": "   ", "trigger": "button"}) is False
    assert nr.real_request({"trigger": None, "stt_text": "turn the lights off"}) is False
    monkeypatch.setenv("EM_CUE_ADDRESS", "hey jarvis, computer, a, on")
    assert nr.real_request({
        "stt_text": "hey jarvis turn the lights off",
        "wake_model": "hey_jarvis_v0.1",
    }) is True
    assert nr.real_request({"stt_text": "computer what is the time"}) is True
    assert nr.real_request({
        "stt_text": "turn the lights off", "wake_model": "ab_v0.1",
    }) is False
    # "a" and "on" are too short to be an address, so they match nothing.
    assert nr.real_request({"stt_text": "turn on the lamp in the hall"}) is False
    monkeypatch.setenv("EM_CUE_MIN_WORDS", "nope")
    monkeypatch.setenv("EM_CUE_MIN_CHARS", "3")
    assert nr.real_request({
        "stt_text": "lights on", "trigger": "button",
    }) is True
    monkeypatch.setenv("EM_CUE_MIN_WORDS", "4")
    monkeypatch.setenv("EM_CUE_MIN_CHARS", "8")
    assert nr.real_request({
        "stt_text": "lights on", "trigger": "button",
    }) is False


def test_a_verifier_or_speaker_match_can_count(monkeypatch):
    monkeypatch.delenv("EM_CUE_WAKE_SCORE", raising=False)
    weak = {"wake_score": 0.2, "stt_text": ""}
    assert nr.real_request(dict(weak)) is False
    for key in nr.HOOK_FIELDS:
        turn = dict(weak)
        turn[key] = True
        assert nr.real_request(turn) is True
        turn[key] = "true"
        assert nr.real_request(turn) is False
        turn[key] = 1
        assert nr.real_request(turn) is False

    def accept(turn):
        return turn.get("who") == "family"

    nr.address_hook = accept
    assert nr.real_request({"who": "family", "wake_score": 0.1}) is True
    assert nr.real_request({"who": "tv", "wake_score": 0.1}) is False
    nr.address_hook = lambda turn: "yes"  # anything other than True stays silent
    assert nr.real_request(weak) is False

    def boom(turn):
        raise RuntimeError("verifier down")

    nr.address_hook = boom
    assert nr.real_request(weak) is False


def test_the_tone_is_short_audible_pcm():
    for kind in ("miss", "fallback"):
        pcm = nr.cue_pcm(kind)
        assert len(pcm) % 2 == 0
        assert 0 < len(pcm) < nr.RATE * 2  # under a second
        assert any(pcm[i:i+2] != b"\x00\x00" for i in range(0, len(pcm), 2))
    assert nr.PHRASE["miss"] == "didn't catch that"
    assert "Home Assistant" in nr.PHRASE["fallback"]


def test_stamp_records_the_reason_and_play_speaks_only_a_real_request(caplog):
    class Dev:
        device_id = "G090"
        barge_detected = False
        last_fail_reason = None
        last_real_request = False
        pcm = None

        async def stream_speaker(self, pcm):
            self.pcm = pcm

    dev = Dev()
    rec = {"outcome": "no_tts", "stt_text": "", "wake_score": 0.4}
    caplog.set_level(logging.INFO, logger="echomuse")
    assert nr.stamp(dev, rec) == "no_tts"
    assert rec["fail_reason"] == "no_stt"
    assert dev.last_fail_reason == "no_stt"
    assert dev.last_real_request is False
    assert "reason=no_stt" in caplog.text
    assert "didn't catch that" not in caplog.text

    import asyncio
    asyncio.run(nr.play(dev))
    assert dev.pcm is None
    assert dev.last_fail_reason is None
    assert dev.last_real_request is False
    assert "didn't catch that" not in caplog.text

    heard = {
        "outcome": "no_tts", "stt_text": "", "wake_score": 0.91,
        "trigger": "wakeword(0.910)",
    }
    nr.stamp(dev, heard)
    assert heard["fail_reason"] == "no_stt"
    assert dev.last_real_request is True
    asyncio.run(nr.play(dev))
    assert dev.pcm
    assert "didn't catch that" in caplog.text
    assert dev.last_fail_reason is None

    dev.barge_detected = True
    dev.last_fail_reason = "timeout"
    dev.last_real_request = True
    dev.pcm = None
    asyncio.run(nr.play(dev))
    assert dev.pcm is None
    assert dev.last_fail_reason is None

    dev.barge_detected = False
    dev.last_fail_reason = "no_speech"
    dev.last_real_request = True
    asyncio.run(nr.play(dev))
    assert dev.pcm is None

    # The reply wait still speaks when the wake was a poor one.
    dev.last_fail_reason = "timeout"
    dev.last_real_request = False
    dev.pcm = None
    asyncio.run(nr.play(dev))
    assert dev.pcm
    assert "no reply from Home Assistant" in caplog.text

    def boom(pcm):
        raise RuntimeError("speaker down")

    async def broken(pcm):
        boom(pcm)

    dev.stream_speaker = broken
    dev.last_fail_reason = "no_tts"
    dev.last_real_request = True
    asyncio.run(nr.play(dev))
    assert dev.last_fail_reason is None
    assert dev.last_real_request is False


def test_a_near_miss_with_empty_stt_ends_quietly(caplog):
    class Dev:
        device_id = "G090"
        barge_detected = False
        last_fail_reason = None
        last_real_request = False
        gate_near_miss = True
        pcm = None

        async def stream_speaker(self, pcm):
            self.pcm = pcm

    caplog.set_level(logging.INFO, logger="echomuse")
    dev = Dev()
    rec = {
        "outcome": "no_tts", "stt_text": "  ", "wake_score": 0.95,
        "trigger": "wakeword(0.950)",
    }
    nr.stamp(dev, rec)
    assert rec["gate_near_miss"] is True
    assert dev.gate_near_miss is False
    assert rec["fail_reason"] == "no_stt"
    assert dev.last_real_request is False
    assert "reason=no_stt" in caplog.text
    import asyncio
    asyncio.run(nr.play(dev))
    assert dev.pcm is None
    assert "didn't catch that" not in caplog.text

    dev.gate_near_miss = True
    heard = {
        "outcome": "no_tts",
        "stt_text": "hey jarvis what time is it",
        "wake_score": 0.95,
        "trigger": "wakeword(0.950)",
        "wake_model": "hey_jarvis_v0.1",
    }
    nr.stamp(dev, heard)
    assert heard["gate_near_miss"] is True
    assert dev.last_real_request is True


def test_a_stored_decision_is_what_play_uses():
    """cue_kind honours the bool stamp already decided, including a false one."""
    assert nr.cue_kind("no_tts", {"wake_score": 0.99}, real=False) == ""
    assert nr.cue_kind("no_stt", real=True) == "miss"


def test_a_successful_turn_stores_a_null_reason(tmp_path):
    em_db.init(str(tmp_path / "em.db"))
    try:
        class Dev:
            device_id = "G090LF0000000001"
            last_fail_reason = "stale"
        dev = Dev()
        ok = {"outcome": "ok", "stt_text": "hi", "ts": 1.0}
        nr.stamp(dev, ok)
        assert ok["fail_reason"] is None
        assert dev.last_fail_reason is None
        em_db.insert_turn(dev.device_id, ok)
        failed = {"outcome": "no_tts", "stt_text": "lights", "ts": 2.0,
                  "fail_reason": "no_tts"}
        em_db.insert_turn(dev.device_id, failed)
        rows = em_db.get_turns(dev.device_id)
        assert rows[0]["fail_reason"] is None
        assert rows[1]["fail_reason"] == "no_tts"
    finally:
        em_db.close()


class _Cue:
    """A device the tone can play to. speaking starts set, as stream_speaker does."""

    def __init__(self):
        self.device_id = "G090"
        self.barge_detected = False
        self.last_fail_reason = "no_tts"
        self.last_real_request = True
        self.speaking = True
        self.speaking_since = 0.0
        self.pcm = None
        self.cleared = 0

    async def stream_speaker(self, pcm):
        self.pcm = pcm
        self.speaking = True

    async def _set_speaking(self, value):
        self.speaking = value
        self.speaking_since = None if not value else self.speaking_since
        if not value:
            self.cleared += 1


def test_a_cue_that_finishes_clears_speaking():
    dev = _Cue()
    asyncio.run(nr.play(dev))
    assert dev.pcm
    assert dev.speaking is False
    assert dev.speaking_since is None
    assert dev.cleared == 1


def test_a_cue_that_errors_clears_speaking(caplog):
    dev = _Cue()

    async def broken(pcm):
        dev.pcm = pcm
        raise RuntimeError("speaker down")

    dev.stream_speaker = broken
    caplog.set_level(logging.WARNING, logger="echomuse")
    asyncio.run(nr.play(dev))
    assert dev.speaking is False
    assert dev.cleared == 1
    assert "no-reply tone failed" in caplog.text
    assert "speaker down" in caplog.text


def test_a_cue_that_hangs_clears_speaking_at_five_seconds(caplog, monkeypatch):
    monkeypatch.setattr(nr, "CUE_TIMEOUT_S", 0.05)
    dev = _Cue()

    async def hang(pcm):
        dev.pcm = pcm
        await asyncio.Event().wait()

    dev.stream_speaker = hang
    caplog.set_level(logging.WARNING, logger="echomuse")
    asyncio.run(nr.play(dev))
    assert dev.speaking is False
    assert dev.cleared == 1
    assert "timed out" in caplog.text
    assert "clearing speaking" in caplog.text


def test_the_cue_limits_are_five_and_thirty_seconds():
    assert nr.CUE_TIMEOUT_S == 5.0
    assert nr.SPEAKING_STUCK_S == 30.0


def test_a_silent_miss_does_not_clear_speaking_a_reply_still_owns():
    dev = _Cue()
    dev.last_real_request = False
    dev.last_fail_reason = "no_stt"
    asyncio.run(nr.play(dev))
    assert dev.pcm is None
    assert dev.speaking is True
    assert dev.cleared == 0


def test_a_failed_speaking_clear_still_drops_the_flag():
    dev = _Cue()

    async def boom(value):
        raise RuntimeError("dashboard gone")

    dev._set_speaking = boom
    asyncio.run(nr.play(dev))
    assert dev.speaking is False
    assert dev.speaking_since is None


def test_the_speaking_watchdog_clears_a_flag_stuck_for_thirty_seconds(caplog):
    fresh = _Cue()
    fresh.speaking_since = 0.0
    caplog.set_level(logging.WARNING, logger="echomuse")
    asyncio.run(nr.clear_stuck_speaking([fresh], now=29.9))
    assert fresh.speaking is True
    assert fresh.cleared == 0

    stuck = _Cue()
    stuck.speaking_since = 0.0
    quiet = _Cue()
    quiet.speaking = False
    quiet.speaking_since = 0.0
    unstamped = _Cue()
    unstamped.speaking_since = None
    flagged = _Cue()
    flagged.speaking_since = True
    asyncio.run(nr.clear_stuck_speaking(
        [stuck, quiet, unstamped, flagged], now=30.0))
    assert stuck.speaking is False
    assert stuck.speaking_since is None
    assert stuck.cleared == 1
    assert quiet.speaking is False
    assert quiet.cleared == 0
    assert unstamped.speaking is True
    assert flagged.speaking is True
    assert "speaking stuck for 30s" in caplog.text
    assert "G090" in caplog.text
    assert "clearing" in caplog.text


def test_the_watchdog_uses_the_clock_when_no_time_is_given():
    dev = _Cue()
    dev.speaking_since = time.monotonic() - 31
    asyncio.run(nr.clear_stuck_speaking([dev]))
    assert dev.speaking is False


def test_a_device_with_no_setter_is_still_cleared():
    class Bare:
        device_id = "G091"
        speaking = True
        speaking_since = 0.0

    dev = Bare()
    asyncio.run(nr.clear_stuck_speaking([dev], now=31.0))
    assert dev.speaking is False
    assert dev.speaking_since is None


def test_the_lag_monitor_runs_the_speaking_watchdog():
    src = Path(__file__).resolve().parents[1].joinpath("em_controller.py").read_text()
    start = src.index("async def event_loop_lag_monitor")
    end = src.index("\nasync def ", start + 1)
    assert "clear_stuck_speaking" in src[start:end]
