"""
em_arbiter.py — multi-device wake arbitration
==============================================

When one utterance wakes more than one Echo (open-plan rooms, hallways),
every device that scored above threshold starts its own voice turn. The
result is not merely duplicated answers: each device then hears the
others' TTS through the room, transcribes it as a follow-up, and HA's
`continue_conversation` reopens the mic — a self-feeding loop that ran
for ~70 seconds on 2026-07-20 before a no-speech timeout broke it.

**First detector wins.** The first device to cross threshold claims the
utterance immediately and answers with zero added latency. Any other
device detecting within `window_s` of that claim loses and stands down.
No one ever waits.

**Only devices that can serve a turn get here.** Detection order is a
proximity proxy and says nothing about whether Home Assistant is connected
to that device, so an Echo with no pipeline behind it would otherwise win
on nearness and then fail — silencing one that was ready. The caller
(`em_controller.wake_word_listener`) stands such a device down before it
claims, via `em_esphome.can_serve_turn`. That check is deliberately NOT
repeated here: this module knows nothing of HA, and a second copy of the
rule is one that can disagree with the first.

Why not "best SNR", which this module did until 2026-07-20: it forced
every wake to wait out the whole window (~364ms measured, on every
device, because the gate was "2+ devices *connected*" rather than "in
earshot"), and the field data showed the metric was wrong anyway. On the
three-way trigger that exposed all this, SNR at detection was
0.9 / 1.15 / 0.93 — statistically indistinguishable — and the SNR winner
(Lounge) produced "What's the technique?" while the *first* detector
(Office) produced the correct "What's the weather like today?". Sound
reaches the nearer microphone sooner and louder, so it crosses threshold
sooner; detection order is a better proximity proxy than a ratio of two
noisy RMS estimates, and it is free.

`window_s` is now purely a suppression window, not a wait: it bounds how
long a claim silences other devices. It should comfortably exceed the
spread between devices hearing one utterance (~200ms observed, driven by
the device's 160ms mic batching) without being so long that a genuinely
separate wake in another room gets swallowed.

**Claims are compared by when the audio was HEARD, not when the claim
arrived** (docs/listening.md, "Arbitration"). An Echo detecting its own wake
reports how long ago it captured the audio, and a message can spend a second
or more in a TCP retransmit on this fleet's links (#139). Compared by arrival,
the near Echo's late claim fell outside the winner's window, read as a new
utterance, and started a second answer. So each claim carries `heard_at`, a
claim loses if it was heard within `window_s` of the winner's, and the
winner's claim is held for `window_s + slack_s` so a late claim still finds
it. A granted claim is never revoked — that would cut off a turn already
listening. Without `heard_at` a claim is heard on arrival, which is exactly
the old behaviour.

**A mixed fleet holds its claims** (Wil, 2026-09-24). When some Echoes
detect the wake word themselves and others are scored here, the two paths
reach this module at different speeds, so first-to-arrive is not
first-to-hear: on 2026-09-24 an Echo 10m away, detecting on the device,
claimed 16ms before the one a metre from the speaker, which was scored here
as a barge-in, and took the turn. `contest()` holds the first claim until
`hold_s` after it was heard, collects every claim heard within the window by
then, and grants the one heard earliest. Nothing is revoked: nobody holds the
utterance until the contest is decided. A fleet whose Echoes all detect the
same way races on equal terms, so it does not wait — `contest()` with
`hold_s=0` is exactly `claim()`.

**Every contest is held, and loudness and time decide it together** (Wil,
2026-10-05 and 2026-10-06, #747). First-to-hear alone turned out to be decided
by timing noise: the scorer works in 80ms frames on a grid that starts at a
different moment on each Echo, so three Echoes side by side, hearing one word
within a millisecond, reported it 89 to 113ms apart (measured 2026-10-06), and
one of them was 53-55ms behind another on every wake. Sound covers a metre in
3ms, so that noise is larger than any real difference in a house. In one
house's twelve contested wakes the nearest Echo heard the word 54 to 164ms
AFTER the winner in four of them, while being 11 to 26dB louder than every
other Echo in all twelve.

So whenever two or more Echoes can claim, the first claim is held `hold_s`
from when it was heard, and `pick()` gives the turn to the loudest. There is
no margin: what has to be right is the ROOM (Wil, 2026-10-06: "if I had
multiple Echoes in the same room I'm probably not going to care which answers
me"), and between rooms the lead is large, 9.7dB at the least in that house's
fourteen contested wakes. Inside one room levels sit within a decibel or two
and the choice between those Echoes is arbitrary, which is acceptable; a
margin would only hand those cases back to the frame grid. An exact tie
breaks on wake score, then on who was heard first. When every claim carries
a noise floor, loudness is speech above that floor (level minus floor);
otherwise it is the level itself.

Loudness is `level` from em_wakelevel, the energy mean over the wake's window:
post-AEC, AGC never runs on the wake stream, and the mic gain setting is
divided out. It moved less from wake to wake than the single loudest frame
did (0.8dB against 1.9dB over ten side-by-side wakes), and on matched capture
settings three Echoes agreed on it to within 2.7dB. It is only used when
it can be trusted: the contest goes back to earliest-heard when any claim has
no level (older firmware), when a claim was heard over that Echo's own
playback (the reading includes the reply or the music), or when the Echoes'
MICPGA and digital gain settings differ (those are not divided out). A weak
microphone is NOT caught here: it reads quiet and would lose to a louder Echo
further away. The answer to that is the per-Echo check against its other six
mics (#731), not a wider margin.

NOT handled yet: an emOS Echo captures about 6dB below a FireOS 5 one (#806),
so in a fleet with both the FireOS Echo is favoured by that much.

This is not the best-SNR-after-a-wait design removed on 2026-07-20. That one
waited on every wake in every fleet and ranked by a ratio that could not tell
Echoes apart (0.9/1.15/0.93). This one measures something that separates them
by 11dB or more, and keeps time as the answer whenever it does not.

Pure asyncio, no imports from the rest of the controller — unit-tested
in tests/test_arbiter.py.

`score_hook`, when set, sees the claims and the loudness winner after
`pick()` and may name a different winner. None leaves `pick()` as the
only decision. The default install logs a shadow score and returns None.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import NamedTuple

log = logging.getLogger("echomuse.arbiter")

# Nearest-Dot contest (2026-10-29). legacy is the 250 ms hold and pick().
# shadow runs that path and logs the nearest winner. nearest waits
# max(heard + hold, arrival + min_wait), capped, and returns pick_nearest().
MODES = ("legacy", "shadow", "nearest")
TIE_DB = 0.05
EARLY_LAG_S = 0.15
DEFAULT_HOLD_S = 0.35
DEFAULT_MIN_WAIT_S = 0.15
DEFAULT_CAP_S = 0.60
DEFAULT_ECHO_MARGIN_DB = 6.0
DEFAULT_ECHO_SOLO_MIN = 0.85
# A clean claim takes an echo-flagged louder Dot only at this SNR, or when
# its level is within DEFAULT_ECHO_HANDOFF_MARGIN_DB of that Dot. 0 and a
# very large handoff margin are the 2.26.8 handoff.
DEFAULT_MIN_SNR_DB = 2.0
DEFAULT_ECHO_HANDOFF_MARGIN_DB = 6.0
# A suspect with no playback reference that leads the best clean claim by
# more than this, and scores at the solo bar, is the person. Below it the
# handoff stands. Contest 142's 6.7 dB lead stays a handoff.
DEFAULT_ECHO_OVERRIDE_DB = 10.0
# Own playback that ended more than this long before the wake is not
# "over playback". The device barge flag can still be set after the
# controller has already logged playback complete.
DEFAULT_PLAYBACK_TAIL_S = 0.5
# A lone claim leaves at the early mark only at this SNR, and only when it
# is not an echo. Below the bar the contest waits out the hold. 0 with the
# gate off is the arrival-based early exit from 2.26.9.
DEFAULT_EARLY_MIN_SNR_DB = 8.0
DEFAULT_TAKEOVER_MAX_S = 0.25
DEFAULT_TAKEOVER_MARGIN_DB = 6.0
# Home Assistant drops a second pipeline for the same phrase inside 2 s
# (WAKE_WORD_COOLDOWN). 2.05 clears that edge.
DEFAULT_HA_WAKE_COOLDOWN_S = 2.05
DEFAULT_LOG = "/share/leo/logs/wake-arbitration.jsonl"

# (entries, loudness_winner) -> device id, or None to keep the loudness winner.
# Assigned by em_reliability. This module does not import that one.
score_hook = None


class Claim(NamedTuple):
    heard: float
    device_id: str
    # em_wakelevel's level for this wake, dBFS; None when the Echo sent none.
    level: float | None = None
    # The gain settings the reading still contains (MICPGA, digital gain).
    gains: tuple | None = None
    # Heard over this Echo's own speaker output.
    over_playback: bool = False
    # Wake score, used only when loudness ties.
    score: float | None = None
    # Noise floor in the same dBFS scale as `level`. SNR is level - floor.
    floor_db: float | None = None
    # Loudest frame in the wake window, dBFS. Logged; pick() does not use it.
    peak: float | None = None
    # When this claim reached the controller, loop clock. None means `heard`.
    arrived: float | None = None
    # This Dot's own playback reference, dBFS. Echo check only.
    playback_ref: float | None = None
    # Another Dot, or a same-room player, was playing.
    external_playing: bool = False
    room: str = ""


def _primary(claim: Claim, use_snr: bool) -> float:
    """Speech above the noise floor when every claim has one, else the level."""
    if use_snr:
        return claim.level - claim.floor_db
    return claim.level


def pick(claims: list) -> tuple:
    """
    (winner, why) among claims for one utterance.

    The loudest wins: speech level above the noise floor when every claim
    carries a floor, otherwise the level itself. A louder room beats a
    higher score and an earlier hearing. Equal loudness breaks on wake
    score, then on who was heard first. Without levels that can be compared
    the earliest heard wins. `why` is for the log line.
    """
    earliest = min(claims, key=lambda c: c.heard)
    if len(claims) < 2:
        return earliest, "only claim"
    if any(c.level is None for c in claims):
        return earliest, "earliest heard; a level is missing"
    if any(c.over_playback for c in claims):
        return earliest, "earliest heard; one was heard over playback"
    if len({c.gains for c in claims}) > 1:
        return earliest, "earliest heard; gain settings differ"
    use_snr = all(c.floor_db is not None for c in claims)
    # Heard order first, then loudness, so an equal level keeps the claim
    # that was heard first. Both sorts are stable.
    ordered = sorted(claims, key=lambda c: c.heard)
    ordered.sort(key=lambda c: _primary(c, use_snr), reverse=True)
    best, second = ordered[0], ordered[1]
    lead = _primary(best, use_snr) - _primary(second, use_snr)
    if (lead == 0 and best.score is not None and second.score is not None
            and best.score != second.score):
        tied = [c for c in ordered
                if _primary(c, use_snr) == _primary(best, use_snr)
                and c.score is not None]
        best = max(tied, key=lambda c: (c.score, -c.heard))
        return best, f"wake score {best.score:.2f}"
    unit = "dB SNR" if use_snr else "dB"
    return best, f"loudest by {lead:.1f}{unit}"


def arbitration_mode(env=None) -> str:
    """`legacy`, `shadow`, or `nearest`. Anything else is shadow."""
    raw = os.environ if env is None else env
    text = str(raw.get("EM_ARBITRATION_MODE", "") or "").strip().lower()
    if text in MODES:
        return text
    return "shadow"


def _bool_opt(raw, default: bool) -> bool:
    """Missing or blank keeps `default`. 0/false/off is off; 1/true/on is on."""
    if raw is None or str(raw).strip() == "":
        return default
    text = str(raw).strip().lower()
    if text in ("0", "false", "off", "no"):
        return False
    if text in ("1", "true", "on", "yes"):
        return True
    return default


def _float_opt(raw, default, lo, hi):
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, value))


def room_offsets(env=None) -> dict:
    """Serial → dB. Missing, empty, or unreadable is an empty map."""
    raw = os.environ if env is None else env
    text = str(raw.get("EM_ARBITRATION_ROOM_OFFSETS_DB", "") or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def nearest_settings(env=None) -> dict:
    """Options the contest reads. Times are seconds."""
    raw = os.environ if env is None else env
    mode = arbitration_mode(raw)
    return {
        "mode": mode,
        "hold_s": _float_opt(raw.get("EM_ARBITRATION_HOLD_MS"), 350, 0, 2000) / 1000.0,
        "min_wait_s": _float_opt(raw.get("EM_ARBITRATION_MIN_WAIT_MS"), 150, 0, 2000) / 1000.0,
        "cap_s": _float_opt(raw.get("EM_ARBITRATION_CAP_MS"), 600, 1, 5000) / 1000.0,
        "offsets": room_offsets(raw),
        "echo_margin_db": _float_opt(
            raw.get("EM_ECHO_MARGIN_DB"), DEFAULT_ECHO_MARGIN_DB, 0, 40),
        "echo_solo_min_score": _float_opt(
            raw.get("EM_ECHO_SOLO_MIN_SCORE"), DEFAULT_ECHO_SOLO_MIN, 0, 1),
        "min_snr_db": _float_opt(
            raw.get("EM_ARBITRATION_MIN_SNR_DB"), DEFAULT_MIN_SNR_DB, 0, 40),
        "echo_handoff_margin_db": _float_opt(
            raw.get("EM_ECHO_HANDOFF_MARGIN_DB"),
            DEFAULT_ECHO_HANDOFF_MARGIN_DB, 0, 80),
        "echo_override_db": _float_opt(
            raw.get("EM_ECHO_OVERRIDE_DB"),
            DEFAULT_ECHO_OVERRIDE_DB, 0, 80),
        "playback_tail_s": _float_opt(
            raw.get("EM_PLAYBACK_TAIL_MS"),
            DEFAULT_PLAYBACK_TAIL_S * 1000, 0, 5000) / 1000.0,
        "early_snr_gate": _bool_opt(raw.get("EM_ARBITRATION_EARLY_SNR_GATE"), True),
        "early_min_snr_db": _float_opt(
            raw.get("EM_ARBITRATION_EARLY_MIN_SNR_DB"),
            DEFAULT_EARLY_MIN_SNR_DB, 0, 80),
        "takeover": _bool_opt(raw.get("EM_ARBITRATION_TAKEOVER"), True),
        "takeover_max_s": _float_opt(
            raw.get("EM_ARBITRATION_TAKEOVER_MAX_MS"), 250, 0, 2000) / 1000.0,
        "takeover_margin_db": _float_opt(
            raw.get("EM_ARBITRATION_TAKEOVER_MARGIN_DB"),
            DEFAULT_TAKEOVER_MARGIN_DB, 0, 40),
        "ha_wake_cooldown": _bool_opt(raw.get("EM_HA_WAKE_COOLDOWN"), True),
        "ha_wake_cooldown_s": _float_opt(
            raw.get("EM_HA_WAKE_COOLDOWN_S"),
            DEFAULT_HA_WAKE_COOLDOWN_S, 0, 10),
        "log_path": str(raw.get("EM_WAKE_ARBITRATION_LOG", DEFAULT_LOG) or ""),
    }


def close_reason_for(heard: float, arrived: float, hold_s: float,
                     min_wait_s: float, cap_s: float | None, close: float,
                     early: bool) -> str:
    """Why the contest stopped waiting: early, hold, min_wait, or cap."""
    if early:
        return "early"
    hold_at = heard + hold_s
    wait_at = arrived + min_wait_s
    if cap_s is not None and cap_s > 0:
        cap_at = arrived + cap_s
        if close <= cap_at + 1e-9 and cap_at <= max(hold_at, wait_at) + 1e-9:
            if abs(close - cap_at) <= 1e-6:
                return "cap"
    if hold_at + 1e-9 >= wait_at:
        return "hold"
    return "min_wait"


def nearest_close(heard: float, arrived: float, hold_s: float,
                   min_wait_s: float, cap_s: float | None) -> float:
    """Loop-clock instant the contest closes.

    max(heard + hold, arrival + min_wait), and never later than arrival + cap.
    """
    close = max(heard + hold_s, arrived + min_wait_s)
    if cap_s is not None and cap_s > 0:
        close = min(close, arrived + cap_s)
    return close


def _metric(claim: Claim, use_snr: bool, offsets: dict) -> float:
    base = (claim.level - claim.floor_db) if use_snr else claim.level
    return base + float(offsets.get(claim.device_id, 0.0) or 0.0)


def _suspect(claim: Claim, margin: float) -> bool:
    """Playing, and the wake level sits on that playback."""
    if claim.level is None:
        return False
    playing = claim.over_playback or claim.external_playing
    if not playing:
        return False
    if claim.playback_ref is None:
        return bool(claim.over_playback or claim.external_playing)
    return abs(claim.level - claim.playback_ref) <= margin


def _adj_snr(claim: Claim, offsets: dict) -> float | None:
    if claim.level is None or claim.floor_db is None:
        return None
    return (claim.level - claim.floor_db) + float(offsets.get(claim.device_id, 0.0) or 0.0)


def playback_still_counts(playback_idle_at, wake_at, tail_s: float) -> bool:
    """Whether a barge or speaking flag is still this Dot's own playback.

    No recorded end means the flag stands. An end more than `tail_s` before
    the wake does not: the reply is over, and the flag is late.
    """
    if playback_idle_at is None or wake_at is None:
        return True
    return (wake_at - playback_idle_at) <= tail_s


def _echo_override(claim: Claim, clean_best: float, use_snr: bool,
                   offsets: dict, echo_override_db: float,
                   echo_solo_min_score: float) -> bool:
    """A loud suspect with no playback reference is kept in the contest."""
    if claim.playback_ref is not None:
        return False
    if claim.score is None or claim.score < echo_solo_min_score:
        return False
    return _metric(claim, use_snr, offsets) - clean_best > echo_override_db


def _handoff_ok(claim: Claim, echo: Claim, offsets: dict,
                min_snr_db: float, handoff_margin_db: float) -> bool:
    """A clean claim may take the turn from a louder echo-flagged Dot."""
    snr = _adj_snr(claim, offsets)
    close = (
        echo.level is not None and claim.level is not None
        and abs(claim.level - echo.level) <= handoff_margin_db)
    return (snr is not None and snr >= min_snr_db) or close


def _echo_kept(echo: Claim, echo_solo_min_score: float,
               verify_accept) -> tuple:
    """The echo-flagged Dot answers, or the contest is dropped."""
    if echo.score is not None and echo.score >= echo_solo_min_score:
        return echo, "echo_score"
    accepted = False
    if verify_accept is not None:
        try:
            accepted = bool(verify_accept(echo.device_id))
        except Exception:
            accepted = False
    if accepted:
        return echo, "echo_verify"
    return None, "echo_no_clean_claim"


def pick_nearest(claims: list, offsets: dict | None = None,
                  echo_margin_db: float = DEFAULT_ECHO_MARGIN_DB,
                  echo_solo_min_score: float = DEFAULT_ECHO_SOLO_MIN,
                  min_snr_db: float = DEFAULT_MIN_SNR_DB,
                  echo_handoff_margin_db: float = DEFAULT_ECHO_HANDOFF_MARGIN_DB,
                  verify_accept=None,
                  echo_override_db: float = DEFAULT_ECHO_OVERRIDE_DB,
                  ) -> tuple:
    """(winner or None, why) for nearest-Dot arbitration.

    None means nobody answers (`all_echo`, or `echo_no_clean_claim` when a
    louder echo has no clean claim that heard the user). `why` is the log
    reason. `verify_accept(device_id)` is the second-stage checker, already
    finished; a missing result does not answer.
    """
    offsets = offsets or {}
    earliest = min(claims, key=lambda c: (c.heard, c.device_id))
    if any(c.level is None for c in claims):
        return earliest, "fallback:no-level"
    margin = echo_margin_db
    marked = [(c, _suspect(c, margin)) for c in claims]
    eligible = [c for c, suspect in marked if not suspect]
    suspects = [c for c, suspect in marked if suspect]
    if not eligible:
        solo = claims[0] if len(claims) == 1 else None
        if (solo is not None and solo.score is not None
                and solo.score >= echo_solo_min_score):
            return solo, "only"
        return None, "all_echo"
    if len(claims) == 1:
        return eligible[0], "only"
    use_snr = all(c.floor_db is not None for c in claims)
    if eligible and suspects:
        clean_best = max(_metric(c, use_snr, offsets) for c in eligible)
        still = []
        for claim in suspects:
            if _echo_override(claim, clean_best, use_snr, offsets,
                              echo_override_db, echo_solo_min_score):
                eligible.append(claim)
            else:
                still.append(claim)
        suspects = still
    if suspects:
        echo_best = max(suspects, key=lambda c: _metric(c, use_snr, offsets))
        clean_best = max(_metric(c, use_snr, offsets) for c in eligible)
        if _metric(echo_best, use_snr, offsets) >= clean_best:
            eligible = [
                c for c in eligible
                if _handoff_ok(c, echo_best, offsets, min_snr_db,
                               echo_handoff_margin_db)]
            if not eligible:
                return _echo_kept(echo_best, echo_solo_min_score, verify_accept)
    best_adj = max(_metric(c, use_snr, offsets) for c in eligible)
    tied = [c for c in eligible if abs(_metric(c, use_snr, offsets) - best_adj) <= TIE_DB]
    if len(tied) == 1:
        return tied[0], "loudest"
    scores = [c.score for c in tied]
    if all(s is not None for s in scores) and len(set(scores)) > 1:
        winner = max(tied, key=lambda c: (c.score, -c.heard, _neg_serial(c)))
        return winner, "tie:score"
    heards = {c.heard for c in tied}
    if len(heards) > 1:
        winner = min(tied, key=lambda c: (c.heard, c.device_id))
        return winner, "tie:heard"
    winner = min(tied, key=lambda c: c.device_id)
    return winner, "tie:serial"


def _neg_serial(claim: Claim):
    # max() cannot take a lowest string. Rank by inverted code points.
    return tuple(-ord(ch) for ch in claim.device_id)


def _snr(claim: Claim):
    if claim.level is None or claim.floor_db is None:
        return None
    return claim.level - claim.floor_db


def decision_record(contest_id: int, claims: list, winner: str | None,
                    reason: str, opened_heard: float, opened_arrived: float,
                    hold_s: float, min_wait_s: float, cap_s: float | None,
                    decided_at: float, offsets: dict | None = None,
                    echo_margin_db: float = DEFAULT_ECHO_MARGIN_DB,
                    early_exit: bool = False,
                    close_reason: str | None = None) -> dict:
    """One contest, as the JSON line the log stores."""
    offsets = offsets or {}
    use_snr = claims and all(c.level is not None and c.floor_db is not None for c in claims)
    rows = []
    for claim in claims:
        lag = None
        if claim.arrived is not None:
            lag = claim.arrived - claim.heard
        adj = None
        if claim.level is not None:
            adj = _metric(claim, use_snr, offsets)
        rows.append({
            "serial": claim.device_id,
            "room": claim.room,
            "score": claim.score,
            "level": claim.level,
            "peak": claim.peak,
            "floor": claim.floor_db,
            "snr": _snr(claim),
            "offset": float(offsets.get(claim.device_id, 0.0) or 0.0),
            "adj": adj,
            "heard_at": claim.heard,
            "arrival_lag_s": lag,
            "over_playback": claim.over_playback,
            "suspect_echo": _suspect(claim, echo_margin_db) if claim.level is not None else False,
        })
    return {
        "contest_id": contest_id,
        "claims": rows,
        "winner": winner,
        "reason": reason,
        "window": {
            "hold_s": hold_s,
            "min_wait_s": min_wait_s,
            "cap_s": cap_s,
            "opened_heard": opened_heard,
            "opened_arrived": opened_arrived,
        },
        "decided_at": decided_at,
        "wall_time": time.time(),
        "early_exit": bool(early_exit),
        "close_reason": close_reason,
    }


class HaWakeCooldown:
    """Last Home Assistant pipeline start for each wake phrase.

    A second Dot inside `cooldown_s` waits, and the earlier pipeline is
    cancelled when it has not produced a transcript. `enabled` false skips it.
    """

    def __init__(self) -> None:
        self._by_phrase: dict = {}

    def reset(self) -> None:
        self._by_phrase.clear()

    def note_start(self, phrase: str, device_id: str, now: float) -> None:
        if not phrase:
            return
        self._by_phrase[phrase] = {"device": device_id, "started": now, "stt": False}

    def note_stt(self, phrase: str, device_id: str) -> None:
        row = self._by_phrase.get(phrase)
        if row is not None and row["device"] == device_id:
            row["stt"] = True

    def plan(self, phrase: str, device_id: str, now: float, cooldown_s: float,
             enabled: bool = True) -> dict:
        if not enabled or not phrase or cooldown_s <= 0:
            return {"wait_s": 0.0, "cancel": None}
        row = self._by_phrase.get(phrase)
        if row is None or row["device"] == device_id:
            return {"wait_s": 0.0, "cancel": None}
        remain = cooldown_s - (now - row["started"])
        if remain <= 0:
            return {"wait_s": 0.0, "cancel": None}
        return {"wait_s": remain, "cancel": None if row["stt"] else row["device"]}

    def on_duplicate(self, phrase: str, now: float, cooldown_s: float) -> float:
        """Seconds to wait before retrying a duplicate_wake_up_detected start."""
        row = self._by_phrase.get(phrase)
        if row is None:
            return 0.0
        return max(0.0, cooldown_s - (now - row["started"]))


def buffer_allows(qsize: int, maxsize: int, wait_s: float, frame_s: float = 0.08) -> bool:
    """False when the mic queue would fill before the cooldown wait ends."""
    if maxsize <= 0 or wait_s <= 0:
        return True
    incoming = int(wait_s / frame_s) + 1
    return qsize + incoming <= maxsize


ha_cooldown = HaWakeCooldown()


def contest_switches(settings: dict) -> dict:
    """Kwargs contest() reads for the hold gate, the takeover, and nothing else."""
    keys = (
        "early_snr_gate", "early_min_snr_db", "takeover",
        "takeover_max_s", "takeover_margin_db", "echo_override_db",
    )
    return {key: settings[key] for key in keys}


def note_pipeline_error(satellite, data: dict) -> None:
    """Home Assistant pipeline error. A duplicate wake is retried once."""
    code = data.get("code", "")
    message = data.get("message", "")
    log.warning(f"[{satellite._log_name}] Pipeline error: {code} — {message}")
    if (code == "duplicate_wake_up_detected"
            and not getattr(satellite, "_duplicate_retried", False)
            and nearest_settings()["ha_wake_cooldown"]):
        satellite._duplicate_retried = True
        satellite._duplicate_retry = True
    satellite._pipeline_error = True
    satellite._ha_audio_closed = True
    satellite._ha_vad_end.set()
    satellite._tts_event.set()
    satellite._tts_abort.set()


async def guard_ha_start(device, phrase: str, cancel_turn) -> bool:
    """Wait out HA's per-phrase cooldown, then mark the pipeline as started.

    False: the mic queue cannot hold the wait. The turn logs ha_cooldown
    and does not call Home Assistant. Frames already queued stay queued.
    """
    import em_state
    settings = nearest_settings()
    if phrase and settings["ha_wake_cooldown"]:
        now = time.monotonic()
        plan = ha_cooldown.plan(
            phrase, device.device_id, now, settings["ha_wake_cooldown_s"], True)
        wait_s = plan["wait_s"]
        if wait_s > 0 and not buffer_allows(
                device.voice_queue.qsize(), device.voice_queue.maxsize, wait_s):
            log.info("[%s] reason=ha_cooldown — mic buffer cannot hold %.0fms",
                     device.device_id, wait_s * 1000)
            device.last_turn_outcome = "ha_cooldown"
            return False
        if plan["cancel"]:
            cancel_turn(plan["cancel"], abort_ha=True, reason="ha_cooldown")
        if wait_s > 0:
            log.info("[%s] waiting %.0fms for HA wake cooldown on %r",
                     device.device_id, wait_s * 1000, phrase)
            await asyncio.sleep(wait_s)
        ha_cooldown.note_start(phrase, device.device_id, time.monotonic())
    em_state.wake_arbiter.mark_pipeline_started()
    return True


async def after_ha_start(device, satellite, phrase, trace, run_turn,
                         preroll_discard, on_thinking, post_turn_play) -> None:
    """Retry once on duplicate_wake_up_detected, or record that STT arrived."""
    if phrase and getattr(satellite, "_duplicate_retry", False):
        settings = nearest_settings()
        delay = ha_cooldown.on_duplicate(
            phrase, time.monotonic(), settings["ha_wake_cooldown_s"])
        log.info("[%s] reason=ha_cooldown duplicate wake; retrying in %.0fms",
                 device.device_id, delay * 1000)
        if delay > 0:
            await asyncio.sleep(delay)
        satellite._duplicate_retry = False
        ha_cooldown.note_start(phrase, device.device_id, time.monotonic())
        await run_turn(
            device=device, preroll_discard=preroll_discard,
            on_thinking=on_thinking, post_turn_play=post_turn_play,
            trace=trace, wake_word_phrase=phrase)
        return
    text = "" if trace is None else (trace.stt_text or "")
    if phrase and text.strip():
        ha_cooldown.note_stt(phrase, device.device_id)


def append_decision(record: dict, path: str | None) -> None:
    """Log one decision. The file write is skipped when its directory is absent."""
    line = json.dumps(record, separators=(",", ":"), default=str)
    log.info("wake-arbitration %s", line)
    if not path:
        return
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            return
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        log.warning("wake-arbitration log was not written")


class WakeArbiter:
    """
    Tracks the in-flight claim on the current utterance.

    Deliberately tiny and synchronous: claim() decides with no await, so
    the winner's turn starts on the same event-loop tick as its wake
    detection — that is the whole point of the redesign.
    """

    def __init__(self) -> None:
        self._winner: str | None = None
        self._heard_at: float = 0.0
        # The open contest, if a held claim is waiting: the heard time that
        # opened it, the Claims so far, and the future every claimant awaits.
        self._contest: tuple[float, list, asyncio.Future] | None = None
        # Why the last contest went the way it did, for the caller's log.
        self.last_why: str = ""
        # Claims the last contest decided among. Empty until one is held.
        self.last_entries: list = []
        # Nearest-Dot answer for the last contest, even when legacy decided.
        self.last_nearest: tuple | None = None
        self.last_record: dict | None = None
        self._seq: int = 0
        self._close_meta = {"early_exit": False, "close_reason": None}
        self._takeover_until = 0.0
        self._takeover_event: asyncio.Event | None = None
        self._takeover_claim: Claim | None = None
        self._granted_claim: Claim | None = None
        self._first_heard = 0.0
        self._pipeline_started = False
        self._takeover_on = False
        self._takeover_margin = DEFAULT_TAKEOVER_MARGIN_DB
        self._takeover_cap: float | None = None
        self._takeover_offsets: dict = {}
        self._takeover_echo_margin = DEFAULT_ECHO_MARGIN_DB
        self._takeover_log: str | None = None

    def claim(self, device_id: str, window_s: float,
              heard_at: float | None = None, slack_s: float = 0.0) -> str:
        """
        Try to claim the current utterance. Returns the winning device_id
        — equal to device_id if this device won and should answer, or
        another device's id if this detection is a duplicate to discard.

        `heard_at` is when this claim's audio was captured, in the event
        loop's clock (em_listen.heard_at); None means now. `slack_s` extends
        how long the winning claim is held, to cover claims still in flight.

        Returns immediately; there is no waiting on either path.
        """
        now = asyncio.get_running_loop().time()
        heard = now if heard_at is None else min(heard_at, now)
        held = (
            self._winner is not None
            and self._winner != device_id
            and abs(heard - self._heard_at) < window_s
            and now - self._heard_at < window_s + max(0.0, slack_s)
        )
        if held:
            return self._winner
        # Either nothing is claimed, the claim has expired, this was heard
        # outside the window (a separate utterance), or this is the same
        # device waking again (a genuinely new utterance in the room that
        # already answered). Re-arm from this claim.
        self._winner = device_id
        self._heard_at = heard
        return device_id

    async def contest(self, device_id: str, window_s: float,
                      heard_at: float | None = None, slack_s: float = 0.0,
                      hold_s: float = 0.0, level: float | None = None,
                      gains: tuple | None = None, over_playback: bool = False,
                      score: float | None = None,
                      floor_db: float | None = None,
                      early_s: float | None = None,
                      mode: str = "legacy",
                      min_wait_s: float = 0.0,
                      cap_s: float | None = None,
                      peak: float | None = None,
                      playback_ref: float | None = None,
                      external_playing: bool = False,
                      offsets: dict | None = None,
                      echo_margin_db: float = DEFAULT_ECHO_MARGIN_DB,
                      echo_solo_min_score: float = DEFAULT_ECHO_SOLO_MIN,
                      min_snr_db: float = DEFAULT_MIN_SNR_DB,
                      echo_handoff_margin_db: float = DEFAULT_ECHO_HANDOFF_MARGIN_DB,
                      echo_override_db: float = DEFAULT_ECHO_OVERRIDE_DB,
                      verify_accept=None,
                      log_path: str | None = None,
                      room: str = "",
                      early_snr_gate: bool = True,
                      early_min_snr_db: float = DEFAULT_EARLY_MIN_SNR_DB,
                      takeover: bool = True,
                      takeover_max_s: float = DEFAULT_TAKEOVER_MAX_S,
                      takeover_margin_db: float = DEFAULT_TAKEOVER_MARGIN_DB) -> str:
        """
        claim(), held so that every Echo that heard the utterance is counted
        and `pick()` chooses the loudest, or failing a usable level the one
        that HEARD it first. See the module docstring.

        Waits until `hold_s` after the first claim of the utterance was heard
        (less any time it already spent getting here), then returns the
        winner to every claimant. A claim arriving after the decision meets
        the granted winner exactly as in claim(). `hold_s <= 0` does not wait.

        `early_s` ends that wait once it has passed with only one claim.
        None and `early_s <= 0` keep the full hold, which is what every
        existing caller does. A second claim before the early mark keeps
        the hold open until `hold_s`, so a louder Echo still inside the
        old window can join. `early_s` is never waited past `hold_s`.
        """
        if hold_s <= 0:
            return self.claim(device_id, window_s, heard_at, slack_s)
        loop = asyncio.get_running_loop()
        now = loop.time()
        heard = now if heard_at is None else min(heard_at, now)

        fresh = Claim(
            heard, device_id, level, gains, over_playback, score, floor_db,
            peak, now, playback_ref, external_playing, room)
        if self._contest is not None:
            opened, entries, decided = self._contest
            if abs(heard - opened) < window_s:
                entries.append(fresh)
                return await asyncio.shield(decided)

        # No contest to join. A granted winner still holds this utterance.
        if (self._winner is not None and self._winner != device_id
                and abs(heard - self._heard_at) < window_s
                and now - self._heard_at < window_s + max(0.0, slack_s)):
            return self._late_claim(
                fresh, window_s, mode, offsets, echo_margin_db, log_path,
                cap_s, takeover_margin_db)

        entries = [fresh]
        decided: asyncio.Future = loop.create_future()
        self._contest = (heard, entries, decided)
        try:
            # None and <= 0 are the full hold. A positive early mark grants
            # a lone claim then; a second claim already in `entries` keeps
            # the rest of the hold open for anyone else inside the window.
            if mode == "nearest":
                await self._wait_nearest(
                    heard, now, hold_s, min_wait_s, cap_s, early_s, entries, loop,
                    early_snr_gate, early_min_snr_db, offsets, echo_margin_db)
            else:
                limit = hold_s
                if early_s is not None and early_s > 0:
                    limit = min(early_s, hold_s)
                await asyncio.sleep(max(0.0, heard + limit - now))
                if limit < hold_s and len(entries) >= 2:
                    await asyncio.sleep(max(0.0, heard + hold_s - loop.time()))
        finally:
            legacy_won, legacy_why = pick(entries)
            near_won, near_why = pick_nearest(
                entries, offsets, echo_margin_db, echo_solo_min_score,
                min_snr_db, echo_handoff_margin_db, verify_accept,
                echo_override_db)
            if mode == "nearest":
                won, self.last_why = near_won, near_why
            else:
                won, self.last_why = legacy_won, legacy_why
            near_id = None if near_won is None else near_won.device_id
            self.last_nearest = (near_id, near_why)
            self.last_entries = list(entries)
            if won is None:
                winner = ""
            else:
                winner = won.device_id
            hook = score_hook
            if hook is not None and mode != "nearest":
                try:
                    alt = hook(self.last_entries, winner)
                except Exception:
                    alt = None
                if alt:
                    winner = alt
            if self._contest is not None and self._contest[2] is decided:
                self._contest = None
            if won is None or not winner:
                self._winner, self._heard_at = None, 0.0
            else:
                self._winner, self._heard_at = winner, won.heard
            self._seq += 1
            if mode in ("nearest", "shadow"):
                why = near_why if mode == "nearest" else (
                    f"shadow:{near_why};legacy:{legacy_why}")
                record = decision_record(
                    self._seq, self.last_entries,
                    winner if mode == "nearest" else near_id,
                    why, heard, now, hold_s, min_wait_s, cap_s, loop.time(),
                    offsets, echo_margin_db,
                    early_exit=self._close_meta.get("early_exit", False),
                    close_reason=self._close_meta.get("close_reason"))
                if mode == "shadow":
                    record["applied_winner"] = winner
                    record["applied_reason"] = legacy_why
                self.last_record = record
                append_decision(record, log_path)
            if (mode == "nearest" and takeover and takeover_max_s > 0
                    and winner and won is not None):
                self._arm_takeover(
                    loop.time() + takeover_max_s, won, cap_s, offsets,
                    echo_margin_db, takeover_margin_db, log_path, heard)
                await self._takeover_window()
                if self._takeover_claim is not None:
                    winner = self._takeover_claim.device_id
                    self._winner = winner
                    self._heard_at = self._takeover_claim.heard
                    self.last_why = "takeover"
            if not decided.done():
                decided.set_result(winner)
        return winner

    def _arm_takeover(self, until, won, cap_s, offsets, echo_margin_db,
                      margin, log_path, first_heard) -> None:
        self._takeover_on = True
        self._takeover_until = until
        self._takeover_claim = None
        self._granted_claim = won
        self._takeover_cap = cap_s
        self._takeover_offsets = offsets or {}
        self._takeover_echo_margin = echo_margin_db
        self._takeover_margin = margin
        self._takeover_log = log_path
        self._first_heard = first_heard
        self._pipeline_started = False
        self._takeover_event = asyncio.Event()

    async def _takeover_window(self) -> None:
        """Hold the grant until a clearer claim, the pipeline, or the cap."""
        event = self._takeover_event
        if event is None:
            return
        loop = asyncio.get_running_loop()
        timeout = max(0.0, self._takeover_until - loop.time())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._takeover_event = None
        self._takeover_on = False

    def mark_pipeline_started(self) -> None:
        """The winner's Home Assistant pipeline has started. Late claims stay losers."""
        self._pipeline_started = True
        event = self._takeover_event
        if event is not None and not event.is_set():
            event.set()

    def _late_claim(self, fresh: Claim, window_s: float, mode: str,
                    offsets, echo_margin_db: float, log_path: str | None,
                    cap_s: float | None, margin: float) -> str:
        """A claim after the grant. A much clearer one can still take the turn."""
        if self._accept_takeover(fresh, cap_s, window_s, margin, offsets, echo_margin_db):
            self._seq += 1
            both = [c for c in (self._granted_claim, fresh) if c is not None]
            record = decision_record(
                self._seq, both, fresh.device_id, "takeover",
                self._first_heard, fresh.arrived or fresh.heard,
                0.0, 0.0, cap_s, fresh.arrived or fresh.heard,
                offsets, echo_margin_db, early_exit=False, close_reason="takeover")
            record["replaced"] = self._winner
            self.last_record = record
            self.last_why = "takeover"
            append_decision(record, log_path or self._takeover_log)
            self._takeover_claim = fresh
            self._winner = fresh.device_id
            self._heard_at = fresh.heard
            event = self._takeover_event
            if event is not None and not event.is_set():
                event.set()
            return fresh.device_id
        if mode in ("nearest", "shadow"):
            self._log_late(fresh, window_s, mode, offsets, echo_margin_db, log_path)
        return self._winner

    def _accept_takeover(self, fresh: Claim, cap_s, window_s, margin,
                         offsets, echo_margin_db) -> bool:
        if not self._takeover_on or self._pipeline_started:
            return False
        if self._takeover_event is None:
            return False
        span = cap_s if cap_s is not None else window_s
        if abs(fresh.heard - self._first_heard) > span:
            return False
        if _suspect(fresh, echo_margin_db):
            return False
        granted = self._granted_claim
        if granted is None:
            return False
        late = _adj_snr(fresh, offsets or {})
        prior = _adj_snr(granted, offsets or {})
        if late is None or prior is None:
            return False
        return late - prior >= margin

    async def _wait_nearest(self, heard, now, hold_s, min_wait_s, cap_s,
                            early_s, entries, loop, early_snr_gate,
                            early_min_snr_db, offsets, echo_margin_db) -> None:
        """A lone claim leaves early only when it is clearly the near one.

        The early mark is heard + early_s. Below early_min_snr_db, or when
        the claim is an echo, the contest waits until `close`.
        """
        lag = now - heard
        close = nearest_close(heard, now, hold_s, min_wait_s, cap_s)
        lone = entries[0]
        snr = _adj_snr(lone, offsets or {})
        suspect = _suspect(lone, echo_margin_db) if lone.level is not None else False
        confident = (
            (not early_snr_gate)
            or (snr is not None and snr >= early_min_snr_db and not suspect))
        if early_s is not None and early_s > 0 and lag < EARLY_LAG_S and confident:
            if early_snr_gate:
                early_at = heard + early_s
            else:
                # Gate off: the 2.26.9 exit, measured from arrival.
                early_at = now + early_s
            early_at = min(early_at, close)
            await asyncio.sleep(max(0.0, early_at - loop.time()))
            if len(entries) < 2:
                self._close_meta = {"early_exit": True, "close_reason": "early"}
                return
        await asyncio.sleep(max(0.0, close - loop.time()))
        self._close_meta = {
            "early_exit": False,
            "close_reason": close_reason_for(
                heard, now, hold_s, min_wait_s, cap_s, close, False),
        }

    def _log_late(self, claim: Claim, window_s: float, mode: str,
                  offsets, echo_margin_db: float, log_path: str | None) -> None:
        self._seq += 1
        record = decision_record(
            self._seq, [claim], self._winner, "late_loser",
            self._heard_at, claim.arrived or claim.heard,
            0.0, 0.0, None, claim.arrived or claim.heard,
            offsets, echo_margin_db)
        record["window_s"] = window_s
        record["mode"] = mode
        self.last_record = record
        append_decision(record, log_path)

    def release(self, device_id: str) -> None:
        """
        Drop a claim once its turn is over, so an immediate follow-up from
        another device isn't suppressed by a stale window. Ignores calls
        from a device that doesn't hold the claim.
        """
        if self._winner == device_id:
            self._winner = None
            self._heard_at = 0.0
