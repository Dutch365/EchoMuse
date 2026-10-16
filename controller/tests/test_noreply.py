"""A failed turn carries a reason, and the silent ones make a sound."""

import os

import pytest

import em_db
import em_noreply as nr


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


def test_the_miss_tone_is_only_for_no_stt_and_no_tts(monkeypatch):
    monkeypatch.delenv("EM_NO_REPLY_CUE", raising=False)
    monkeypatch.delenv("EM_REPLY_FALLBACK", raising=False)
    assert nr.cue_kind("no_stt") == "miss"
    assert nr.cue_kind("no_tts") == "miss"
    assert nr.cue_kind("timeout") == "fallback"
    assert nr.cue_kind("no_speech") == ""
    assert nr.cue_kind("tts_error") == ""
    monkeypatch.setenv("EM_NO_REPLY_CUE", "0")
    monkeypatch.setenv("EM_REPLY_FALLBACK", "0")
    assert nr.cue_kind("no_stt") == ""
    assert nr.cue_kind("timeout") == ""
    assert nr.enabled("EM_NO_REPLY_CUE") is False


def test_zero_is_off_and_the_words_for_on_are_on():
    assert nr.enabled("EM_DOES_NOT_EXIST", True) is True
    os.environ["EM_NO_REPLY_CUE"] = "false"
    try:
        assert nr.enabled("EM_NO_REPLY_CUE") is False
    finally:
        del os.environ["EM_NO_REPLY_CUE"]


def test_the_tone_is_short_audible_pcm():
    for kind in ("miss", "fallback"):
        pcm = nr.cue_pcm(kind)
        assert len(pcm) % 2 == 0
        assert 0 < len(pcm) < nr.RATE * 2  # under a second
        assert any(pcm[i:i+2] != b"\x00\x00" for i in range(0, len(pcm), 2))
    assert nr.PHRASE["miss"] == "didn't catch that"
    assert "Home Assistant" in nr.PHRASE["fallback"]


def test_stamp_records_the_reason_and_play_speaks_it():
    class Dev:
        device_id = "G090"
        barge_detected = False
        last_fail_reason = None
        pcm = None

        async def stream_speaker(self, pcm):
            self.pcm = pcm

    dev = Dev()
    rec = {"outcome": "no_tts", "stt_text": ""}
    assert nr.stamp(dev, rec) == "no_tts"
    assert rec["fail_reason"] == "no_stt"
    assert dev.last_fail_reason == "no_stt"

    import asyncio
    asyncio.run(nr.play(dev))
    assert dev.pcm
    assert dev.last_fail_reason is None

    dev.barge_detected = True
    dev.last_fail_reason = "timeout"
    dev.pcm = None
    asyncio.run(nr.play(dev))
    assert dev.pcm is None
    assert dev.last_fail_reason is None


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
