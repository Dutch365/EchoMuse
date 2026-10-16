"""Why a turn failed, and the short tone that says so out loud.

A turn logged `no_tts` with an empty transcript is a different failure from
one that heard the words and then got no audio. Both used to end in silence.
The reason code is stored and logged for every failure. The tone plays for
no speech-to-text, no speech back, and a reply wait that expired.

There is no speech synthesis in this process. The phrase is the log line;
the Echo plays the tone through the speaker path it already has.
"""

import logging
import os

import numpy as np

log = logging.getLogger("echomuse")

# Outcomes that are not failures. Empty is a row that never recorded one.
_OK = frozenset({"", "ok", "answered", "cancelled", "barged", "superseded"})

PHRASE = {
    "miss": "didn't catch that",
    "fallback": "no reply from Home Assistant",
}

RATE = 48000


def enabled(name, default=True):
    """An add-on bool. Unset keeps `default`. "0" is off — it is not truthy."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def reason_code(outcome, stt_text=""):
    """A short code for a failed turn, or "" when the turn did not fail.

    `no_tts` with no transcript is `no_stt`: nothing was heard. The same
    outcome with text is `no_tts`: the words came back and the audio did not.
    Any other outcome is its own code, so a failure is never uncoded.
    """
    outcome = outcome or ""
    if outcome in _OK:
        return ""
    if outcome == "no_tts" and not (stt_text or "").strip():
        return "no_stt"
    return outcome


def cue_kind(reason):
    """Which tone to play, or "" when this failure stays quiet."""
    if reason in ("no_stt", "no_tts") and enabled("EM_NO_REPLY_CUE", True):
        return "miss"
    if reason == "timeout" and enabled("EM_REPLY_FALLBACK", True):
        return "fallback"
    return ""


def _tone(freq, ms, amp=0.22):
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    env = np.ones(n)
    fade = min(n // 8, int(RATE * 0.008))
    if fade:
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade))
        env[:fade] *= ramp
        env[-fade:] *= ramp[::-1]
    return (np.sin(2 * np.pi * freq * t) * env * amp * 32767).astype(np.int16)


def cue_pcm(kind):
    """48 kHz mono s16le. `miss` descends; `fallback` rises."""
    gap = np.zeros(int(RATE * 0.04), dtype=np.int16)
    if kind == "fallback":
        parts = [_tone(523, 90), _tone(659, 90), _tone(784, 160)]
    else:
        parts = [_tone(880, 140), gap, _tone(587, 180)]
    return np.concatenate(parts).tobytes()


def stamp(device, turn_record):
    """Record the reason on the turn and the device. Returns the outcome."""
    outcome = turn_record.get("outcome") or ""
    reason = reason_code(outcome, turn_record.get("stt_text") or "")
    turn_record["fail_reason"] = reason or None
    device.last_fail_reason = reason or None
    if reason:
        log.info(
            f"[{getattr(device, 'device_id', '?')}] turn failed "
            f"reason={reason} outcome={outcome}"
        )
    return outcome


async def play(device):
    """Play the tone for the failure just recorded, then forget it.

    Clearing the reason here means a later turn-end cannot replay a stale
    cue. A barge owns the speaker; the tone stays off.
    """
    reason = getattr(device, "last_fail_reason", None) or ""
    device.last_fail_reason = None
    if getattr(device, "barge_detected", False):
        return
    kind = cue_kind(reason)
    if not kind:
        return
    phrase = PHRASE[kind]
    log.info(f"[{device.device_id}] {phrase} ({reason})")
    try:
        await device.stream_speaker(cue_pcm(kind))
    except Exception as e:
        log.warning(f"[{device.device_id}] no-reply tone failed: {e}")
