"""Why a turn failed, and the short tone that says so out loud.

The reason code is stored and logged for every failure. The miss tone
plays when that failure looks like a request from the family: a clear
transcript directed at the assistant, a high-confidence wake, or a hook
a later wake verifier or speaker match can set. Background chatter, a
television, a low score, no speech, and anything this module cannot
classify end with the reason in the log and no sound. The reply-timeout
tone does not use this gate.

There is no speech synthesis in this process. The phrase is the log line
when the tone actually plays. The Echo plays the tone through the speaker
path it already has.
"""

import asyncio
import logging
import os
import re
import time

import numpy as np

import em_oww_models

log = logging.getLogger("echomuse")

# Outcomes that are not failures. Empty is a row that never recorded one.
_OK = frozenset({
    "", "ok", "answered", "cancelled", "barged", "superseded",
    # HA ended the turn on purpose with nothing to say ("never mind").
    "silent_reply",
})

PHRASE = {
    "miss": "didn't catch that",
    "fallback": "no reply from Home Assistant",
}

RATE = 48000

# A wake that merely crossed the device's own bar (often 0.5) is not enough.
# Scores are 0–1. Missing, non-numeric, and below this bar stay silent.
DEFAULT_WAKE_SCORE = 0.85
# A transcript shorter than this is a fragment. Both have to hold.
DEFAULT_MIN_WORDS = 2
DEFAULT_MIN_CHARS = 8

# stream_speaker sets speaking and returns when the bytes are written. The
# reply path clears the flag from playback_stats; this tone does not go
# through that path. Five seconds is longer than the tone and shorter than
# a turn someone is waiting on. Thirty seconds is the backstop for any
# other path that sets the flag and never reaches its clear: while it is
# set, a later wake is closed and no turn starts.
CUE_TIMEOUT_S = 5.0
SPEAKING_STUCK_S = 30.0

# Turn fields a later check can set. Only an actual True counts; a missing
# field, 1, and the string "true" stay silent. `address_hook` is the same
# door as a callable: assign `em_noreply.address_hook = fn` and return True
# from it when a wake verifier or a speaker match accepts the turn.
HOOK_FIELDS = ("addressed", "speaker_match", "wake_verified")
address_hook = None

_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)


def enabled(name, default=True):
    """An add-on bool. Unset keeps `default`. "0" is off — it is not truthy."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_num(name, default, lo, hi, kind):
    """A configured threshold. Unset, blank, and unparseable keep `default`."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = kind(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, value))


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


def _normalise(text):
    return " ".join(_LETTERS.findall(str(text or "").lower()))


def _high_wake(score):
    """True when `score` is a real number at or above the configured bar."""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    value = float(score)
    if value != value or value in (float("inf"), float("-inf")):
        return False
    bar = _env_num("EM_CUE_WAKE_SCORE", DEFAULT_WAKE_SCORE, 0.0, 1.0, float)
    return value >= bar


def _clear(text):
    words = text.split()
    if len(words) < _env_num("EM_CUE_MIN_WORDS", DEFAULT_MIN_WORDS, 1, 20, int):
        return False
    letters = sum(len(word) for word in words)
    return letters >= _env_num("EM_CUE_MIN_CHARS", DEFAULT_MIN_CHARS, 1, 80, int)


def _button(trigger):
    if not isinstance(trigger, str):
        return False
    label = trigger.strip().lower()
    return label == "button" or label.startswith("button(")


def _phrases(turn):
    """Address phrases: the add-on list, then this turn's own wake word.

    A one- or two-letter phrase matches too much of ordinary speech, so it
    is ignored. The wake word comes from the model name the turn recorded
    (`hey_jarvis_v0.1` → "hey jarvis"), which is the phrase this device
    listens for.
    """
    found = []
    raw = os.environ.get("EM_CUE_ADDRESS") or ""
    for part in str(raw).split(","):
        phrase = _normalise(part)
        if len(phrase.replace(" ", "")) >= 3:
            found.append(phrase)
    model = turn.get("wake_model")
    if isinstance(model, str) and model.strip():
        phrase = _normalise(em_oww_models.display_name(model))
        if len(phrase.replace(" ", "")) >= 3 and phrase not in found:
            found.append(phrase)
    return found


def _has_phrase(text, phrase):
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text) is not None


def _directed(turn):
    """A clear transcript aimed at the assistant.

    Clear is the word and letter thresholds. Directed is the action button,
    or the transcript containing an address phrase. A long transcript with
    neither is chatter or a television and stays silent.
    """
    text = _normalise(turn.get("stt_text"))
    if not _clear(text):
        return False
    if _button(turn.get("trigger")):
        return True
    return any(_has_phrase(text, phrase) for phrase in _phrases(turn))


def _hook_accepts(turn):
    for key in HOOK_FIELDS:
        if turn.get(key) is True:
            return True
    fn = address_hook
    if fn is None:
        return False
    try:
        return fn(turn) is True
    except Exception:
        log.warning("address hook failed; miss cue stays silent")
        return False


def real_request(turn):
    """True when the miss tone may speak.

    Three ways, any one of them: the hook above, a high-confidence wake, or
    a clear transcript directed at the assistant. A missing score, a short
    transcript, and a hook that is absent or returns anything other than
    True are all False.
    """
    if not isinstance(turn, dict):
        return False
    # Audio we sent only because the verifier accepted and Silero was close.
    # An empty transcript is the end of that turn: no tone, no phrase.
    if turn.get("gate_near_miss") is True and not _normalise(turn.get("stt_text")):
        return False
    if _hook_accepts(turn):
        return True
    if _high_wake(turn.get("wake_score")):
        return True
    return _directed(turn)


def cue_kind(reason, turn=None, real=None):
    """Which tone to play, or "" when this failure stays quiet.

    `real` is the decision `stamp` already made. Without it the turn is
    read here, and a turn we were not given stays silent.
    """
    if reason == "timeout" and enabled("EM_REPLY_FALLBACK", True):
        return "fallback"
    if reason in ("no_stt", "no_tts") and enabled("EM_NO_REPLY_CUE", True):
        is_real = real_request(turn or {}) if real is None else real is True
        if is_real:
            return "miss"
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
    """Record the reason on the turn and the device. Returns the outcome.

    The real-request decision is taken here, while the turn record is still
    in hand, and stored as a bool the tone can read later. The reason is
    logged either way. The phrase is not.
    """
    outcome = turn_record.get("outcome") or ""
    reason = reason_code(outcome, turn_record.get("stt_text") or "")
    turn_record["fail_reason"] = reason or None
    if getattr(device, "gate_near_miss", False):
        turn_record["gate_near_miss"] = True
    device.gate_near_miss = False
    device.last_fail_reason = reason or None
    device.last_real_request = real_request(turn_record)
    if reason:
        spoken = cue_kind(reason, real=device.last_real_request)
        log.info(
            f"[{getattr(device, 'device_id', '?')}] turn failed "
            f"reason={reason} outcome={outcome} cue={spoken or 'silent'}"
        )
    return outcome


def _stuck(devices, now, limit):
    """Devices whose speaking flag has been set for at least `limit` seconds."""
    found = []
    for device in devices:
        since = getattr(device, "speaking_since", None)
        if not getattr(device, "speaking", False):
            continue
        if isinstance(since, bool) or not isinstance(since, (int, float)):
            continue
        age = now - since
        if age >= limit:
            found.append((device, age))
    return found


async def _clear_speaking(device):
    """Clear speaking even when the dashboard push fails or this task is cancelled."""
    clear = getattr(device, "_set_speaking", None)
    try:
        if clear is not None:
            await asyncio.shield(clear(False))
            return
    except BaseException:
        pass
    device.speaking = False
    if hasattr(device, "speaking_since"):
        device.speaking_since = None


async def clear_stuck_speaking(devices, now=None, limit=SPEAKING_STUCK_S):
    """Clear a speaking flag that has outlived any real playback.

    The private listener closes a wake while this flag is set, and that
    close is the whole record: no turn, no error. The cue clears itself.
    This is the backstop for every other path, called once a second from
    the event-loop lag monitor.
    """
    if now is None:
        now = time.monotonic()
    for device, age in _stuck(devices, now, limit):
        log.warning(
            f"[{getattr(device, 'device_id', '?')}] speaking stuck for "
            f"{age:.0f}s — clearing"
        )
        await _clear_speaking(device)


async def play(device):
    """Play the tone for the failure just recorded, then forget it.

    Clearing the reason here means a later turn-end cannot replay a stale
    cue. A barge owns the speaker; the tone stays off. A miss with no
    recorded request stays off, and its phrase is not logged.

    stream_speaker sets speaking and does not clear it. Leaving the flag
    set closes every later wake. It is cleared when the tone returns, when
    the tone raises, and when the send is still going after CUE_TIMEOUT_S.
    """
    reason = getattr(device, "last_fail_reason", None) or ""
    real = getattr(device, "last_real_request", False)
    device.last_fail_reason = None
    device.last_real_request = False
    if getattr(device, "barge_detected", False):
        return
    kind = cue_kind(reason, real=real)
    if not kind:
        return
    phrase = PHRASE[kind]
    log.info(f"[{device.device_id}] {phrase} ({reason})")
    try:
        await asyncio.wait_for(
            device.stream_speaker(cue_pcm(kind)),
            timeout=CUE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            f"[{device.device_id}] no-reply tone timed out after "
            f"{CUE_TIMEOUT_S:.0f}s — clearing speaking"
        )
    except Exception as e:
        log.warning(f"[{device.device_id}] no-reply tone failed: {e}")
    finally:
        await _clear_speaking(device)
