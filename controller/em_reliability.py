"""Voice-reliability switches. Every one defaults to shadow, log, or off.

The loudness winner, the 250 ms hold, the 80 ms early exit, the 7 s
listen cap and the speech gate are unchanged while the switches stay at
those defaults. `live` and `adaptive_endpoint` are opt-in.

Weights for the shadow score, fixed here so a log line can be replayed:

    composite = 1.0 * energy_dbfs
              + 1.5 * snr_db
              + 10  * oww_score
              + 6   * verifier_score
              - 0.05 * arrival_delay_ms

Energy and SNR are dB. A missing noise floor contributes 0 SNR, so a
claim with no floor can still be scored. A missing level cannot: that
claim does not move the shadow winner. Verifier score is 0 until a
checker has one. Arrival delay is milliseconds after the earliest claim
in the contest.

`ADAPTIVE_SNR_DB` (6) is the early-SNR bar under which an explicit
verifier accept may extend the no-speech window. The extension is the
greater of the current window and `adaptive_no_speech_ms`, and it is
never longer than the turn's max listen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import em_arbiter
import em_speechgate
import em_wakeverify

log = logging.getLogger("echomuse")

W_ENERGY = 1.0
W_SNR = 1.5
W_OWW = 10.0
W_VERIFY = 6.0
W_DELAY_PER_MS = 0.05

ADAPTIVE_SNR_DB = 6.0
VAD_STEP = 0.05
HA_READ_S = 0.2

_ARB = ("off", "shadow", "live")
_COOL = ("off", "log", "live")
_EXT = ("off", "log", "live")

# device_id -> (until_monotonic, winner_id)
_cool: dict[str, tuple[float, str]] = {}
# device_id -> {"level": float|None, "state": str|None, "mode": str}
_ext: dict[str, dict] = {}


def reset() -> None:
    """Drop cooldown and media-state notes. Tests start from an empty house."""
    _cool.clear()
    _ext.clear()


def _env(env):
    return os.environ if env is None else env


def _raw(env, name):
    raw = _env(env).get(name)
    if raw is None:
        return ""
    return str(raw).strip()


def _choice(env, name, default, allowed):
    text = _raw(env, name).lower()
    if text in allowed:
        return text
    return default


def _clamp_float(raw, default, lo, hi):
    if raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, value))


def _clamp_int(raw, default, lo, hi):
    if raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, value))


def arb_mode(env=None) -> str:
    return _choice(env, "EM_ARB_SCORE_MODE", "shadow", _ARB)


def cool_mode(env=None) -> str:
    return _choice(env, "EM_LOSER_COOLDOWN_MODE", "log", _COOL)


def ext_mode(env=None) -> str:
    return _choice(env, "EM_EXTERNAL_AUDIO_GATE", "log", _EXT)


def adaptive_enabled(env=None) -> bool:
    return _raw(env, "EM_ADAPTIVE_ENDPOINT").lower() in ("1", "true", "yes", "on")


def nearest_cooldown(env=None) -> bool:
    """Nearest mode enforces the loser cooldown instead of logging it."""
    return em_arbiter.arbitration_mode(env) == "nearest"


def cooldown_s(env=None) -> float:
    return float(_clamp_int(_raw(env, "EM_LOSER_COOLDOWN_S"), 7, 5, 10))


def cooldown_window(env=None) -> float:
    """Seconds a loser stays quiet. Nearest mode uses 3s and enforces it."""
    if nearest_cooldown(env):
        return 3.0
    return cooldown_s(env)


def effective_cool_mode(env=None) -> str:
    if nearest_cooldown(env):
        return "live"
    return cool_mode(env)


def override_wake(env=None) -> float:
    return _clamp_float(_raw(env, "EM_LOSER_OVERRIDE_WAKE"), 0.95, 0.0, 1.0)


def faint_dbfs(env=None) -> float:
    return _clamp_float(_raw(env, "EM_EXTERNAL_AUDIO_FAINT_DBFS"), -40.0, -90.0, -5.0)


def adaptive_no_speech_s(env=None) -> float:
    return _clamp_int(_raw(env, "EM_ADAPTIVE_NO_SPEECH_MS"), 3000, 500, 15000) / 1000.0


def adaptive_vad_step(env=None) -> int:
    return _clamp_int(_raw(env, "EM_ADAPTIVE_VAD_STEP"), 1, 0, 5)


def room_map(env=None) -> dict:
    raw = _raw(env, "EM_ROOM_MEDIA_PLAYERS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def _now() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return 0.0


def composite(claim, earliest: float, verifier: float | None = None) -> float | None:
    """One claim's score, or None when it has no energy to score."""
    if claim.level is None:
        return None
    snr = 0.0 if claim.floor_db is None else (claim.level - claim.floor_db)
    oww = 0.0 if claim.score is None else float(claim.score)
    ver = 0.0 if verifier is None else float(verifier)
    delay_ms = max(0.0, (claim.heard - earliest) * 1000.0)
    return (W_ENERGY * claim.level + W_SNR * snr + W_OWW * oww
            + W_VERIFY * ver - W_DELAY_PER_MS * delay_ms)


def features_of(claims, verifiers=None) -> list[dict]:
    """One dict per claim, in arrival order. For the shadow log and replays."""
    verifiers = verifiers or {}
    earliest = min(c.heard for c in claims)
    rows = []
    for claim in claims:
        snr = None if claim.level is None or claim.floor_db is None else (
            claim.level - claim.floor_db)
        delay_ms = max(0.0, (claim.heard - earliest) * 1000.0)
        ver = verifiers.get(claim.device_id)
        rows.append({
            "device": claim.device_id,
            "energy": claim.level,
            "snr": snr,
            "oww": claim.score,
            "verifier": ver,
            "delay_ms": round(delay_ms, 1),
            "composite": composite(claim, earliest, ver),
        })
    return rows


def evaluate(claims, actual: str, verifiers=None) -> dict:
    """Shadow winner beside the loudness winner. `agree` is the comparison."""
    rows = features_of(claims, verifiers) if claims else []
    scored = [r for r in rows if r["composite"] is not None]
    if len(scored) < 2:
        shadow = actual
    else:
        best = max(scored, key=lambda r: (r["composite"], -r["delay_ms"]))
        shadow = best["device"]
    return {
        "actual": actual,
        "shadow": shadow,
        "agree": shadow == actual,
        "features": rows,
    }


def shadow_line(report: dict) -> str:
    word = "agree" if report["agree"] else "disagree"
    parts = []
    for row in report["features"]:
        parts.append(
            f"{row['device']} energy={row['energy']} snr={row['snr']} "
            f"oww={row['oww']} delay_ms={row['delay_ms']} "
            f"composite={row['composite']}"
        )
    return (f"arb_shadow_winner={report['shadow']} actual={report['actual']} "
            f"{word} " + " ".join(parts))


def contest_hook(entries, winner: str):
    """Arbiter hook. Shadow logs and keeps `winner`. Live may replace it."""
    mode = arb_mode()
    if mode == "off":
        return None
    if cool_mode() != "off":
        record_losers(entries, winner, _now(), cooldown_window())
    if len(entries) < 2:
        return None
    report = evaluate(entries, winner)
    log.info(shadow_line(report))
    if mode == "live" and report["shadow"] != winner:
        return report["shadow"]
    return None


def install() -> None:
    em_arbiter.score_hook = contest_hook


def record_losers(entries, winner: str, now: float, window_s: float) -> None:
    """Losing devices of this contest stay cooled until `now + window_s`."""
    for claim in entries:
        if claim.device_id != winner:
            _cool[claim.device_id] = (now + window_s, winner)


def cooldown_action(device_id: str, score, now: float, accept: bool,
                    mode: str | None = None, override: float | None = None,
                    ) -> str:
    """`allow`, `override`, `would_suppress`, or `suppress`.

    `suppress` is live mode. `would_suppress` is log mode. A score at or
    above the override with an explicit verifier accept is `override`.
    """
    mode = effective_cool_mode() if mode is None else mode
    if mode == "off":
        return "allow"
    override = override_wake() if override is None else override
    until = _cool.get(device_id)
    if until is None or now >= until[0]:
        return "allow"
    if score is not None and float(score) >= override and accept:
        return "override"
    if mode == "live":
        return "suppress"
    return "would_suppress"


def held_winner(device_id: str) -> str:
    row = _cool.get(device_id)
    return "" if row is None else row[1]


async def should_drop(device_id: str, score, env=None, now=None,
                      accept=None) -> bool:
    """True only when live mode drops this cooled claim.

    Log mode writes `would_suppress` and returns False, so the claim
    still enters arbitration. A score at the override waits for an
    in-flight checker only in live mode, and only long enough for that
    check; a missing answer is not an accept.
    """
    mode = effective_cool_mode(env)
    if mode == "off":
        return False
    if now is None:
        now = _now()
    if accept is None and mode == "live" and score is not None and (
            float(score) >= override_wake(env)):
        task = em_wakeverify.inflight(device_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), HA_READ_S)
            except Exception:
                pass
        accept = em_wakeverify.accepted(device_id)
    if accept is None:
        accept = False
    action = cooldown_action(device_id, score, now, bool(accept), mode=mode)
    if action == "would_suppress":
        log.info("[%s] would_suppress loser_cooldown score=%s", device_id, score)
        return False
    if action == "suppress":
        log.debug("[%s] loser_cooldown suppressed score=%s", device_id, score)
        return True
    if action == "override":
        log.info("[%s] loser_cooldown override score=%s", device_id, score)
    return False


def note_claim(device, won_by: str, level_db, floor_db, env=None) -> None:
    """Remember this wake's SNR, and cool a device that just lost."""
    if level_db is not None and floor_db is not None:
        device.early_snr_db = level_db - floor_db
    else:
        device.early_snr_db = None
    if won_by != device.device_id and effective_cool_mode(env) != "off":
        _cool[device.device_id] = (_now() + cooldown_window(env), won_by)
    prime_external(device.device_id, level_db, env)


def room_line(device_id: str, score) -> str:
    text = "none" if score is None else f"{float(score):.3f}"
    return f"[{device_id}] room_oww={text}"


def arm_turn(device, env=None) -> None:
    """Per-turn adaptive gate. Off, or without an explicit accept, clears it."""
    device.adaptive_gate_threshold = None
    if not adaptive_enabled(env):
        return
    device_id = getattr(device, "device_id", "") or ""
    if not em_wakeverify.accepted(device_id):
        return
    snr = getattr(device, "early_snr_db", None)
    if snr is None or snr >= ADAPTIVE_SNR_DB:
        return
    base = em_speechgate.clamp_threshold(
        getattr(device, "speech_gate_threshold", em_speechgate.THRESHOLD))
    device.adaptive_gate_threshold = em_speechgate.clamp_threshold(
        base - VAD_STEP * adaptive_vad_step(env))


def adapted_no_speech(enabled: bool, accept: bool, snr, base: float,
                      adaptive_s: float, max_listen_s: float) -> float:
    """No-speech window after the adaptive rule. `base` when the rule is idle."""
    if not enabled or not accept:
        return base
    if snr is None or snr >= ADAPTIVE_SNR_DB:
        return base
    return min(max(base, adaptive_s), max_listen_s)


def no_speech_limit(device, base: float, env=None) -> float:
    cap = float(getattr(device, "max_listen_s", None) or base)
    return adapted_no_speech(
        adaptive_enabled(env),
        em_wakeverify.accepted(getattr(device, "device_id", "") or ""),
        getattr(device, "early_snr_db", None),
        base, adaptive_no_speech_s(env), cap,
    )


def external_verdict(level_db, media_state, speech_failed: bool,
                     mode: str | None = None, faint: float | None = None) -> str:
    """`allow`, `would_drop`, or `drop`.

    All three have to be true: the room's player is playing, the wake is
    at or under the faint bar, and the speech gate never opened. Any
    missing media state, including a timeout, is `allow`.
    """
    mode = ext_mode() if mode is None else mode
    if mode == "off" or not speech_failed:
        return "allow"
    faint = faint_dbfs() if faint is None else faint
    if level_db is None or level_db > faint:
        return "allow"
    if media_state != "playing":
        return "allow"
    return "drop" if mode == "live" else "would_drop"


def prime_external(device_id: str, level_db, env=None) -> None:
    """Start the media-state read when this wake is faint and a player is mapped.

    Returns immediately. The read has its own 200 ms budget. No map, a
    wake louder than the bar, and `off` do not call Home Assistant.
    """
    mode = ext_mode(env)
    _ext[device_id] = {"level": level_db, "state": None, "mode": mode}
    if mode == "off" or level_db is None or level_db > faint_dbfs(env):
        return
    entity = room_map(env).get(device_id)
    if not entity:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _go():
        _ext[device_id]["state"] = await read_media_state(entity, env=env)

    loop.create_task(_go())


def remember_media(device_id: str, level_db, state, mode: str = "log") -> None:
    """Store a media state the read already finished with. Tests and replays."""
    _ext[device_id] = {"level": level_db, "state": state, "mode": mode}


def external_clause(device) -> str:
    """Suffix for the no-speech log. Empty when the gate would not drop."""
    device_id = getattr(device, "device_id", "") or ""
    info = _ext.get(device_id)
    if not info:
        return ""
    verdict = external_verdict(
        info.get("level"), info.get("state"), True, mode=info.get("mode"))
    if verdict == "allow":
        return ""
    if verdict == "drop":
        return " external_audio drop"
    return " external_audio would_drop"


async def read_media_state(entity_id: str, timeout_s: float = HA_READ_S,
                           client=None, env=None):
    """The player's HA state, or None when the read fails or times out.

    None fails open: the caller does not gate. `client(entity_id)` replaces
    the supervisor request in tests.
    """
    if client is not None:
        try:
            return await asyncio.wait_for(client(entity_id), timeout_s)
        except Exception:
            return None
    token = _raw(env, "SUPERVISOR_TOKEN")
    if not token or not entity_id:
        return None
    try:
        return await asyncio.wait_for(
            _supervisor_state(entity_id, token, timeout_s), timeout_s)
    except Exception:
        return None


async def _supervisor_state(entity_id: str, token: str, timeout_s: float):
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    url = "http://supervisor/core/api/states/" + entity_id
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
                url, headers={"Authorization": "Bearer " + token}) as resp:
            if resp.status != 200:
                return None
            body = await resp.json(content_type=None)
    if not isinstance(body, dict):
        return None
    state = body.get("state")
    return state if isinstance(state, str) else None


install()
