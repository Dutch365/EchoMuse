"""Optional second-stage wake check.

Disabled unless ``WAKE_VERIFY_URL`` is set. A streaming wake (controller
scoring, or shadow) POSTs the pre-roll held by ``em_wake_samples`` — about
1.5s ending at the crossing — with the score, the device id and the frame
RMS. The checker answers ``{"accept": true}`` or ``{"accept": false}``
within ``WAKE_VERIFY_TIMEOUT_MS`` (default 300). A timeout, a transport
error, or a body that is not that object follows ``WAKE_VERIFY_ON_ERROR``,
which defaults to accept, so a checker that is down does not silence the
house.

Private listening has no pre-roll on this side (the Echo opens the session
after its own wake word, and that audio is not fed into the capture ring).
Those wakes are not sent. Nothing here is a default host: the URL is the
operator's.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

import aiohttp

import em_wake_samples

log = logging.getLogger("echomuse.wakeverify")

DEFAULT_TIMEOUT_MS = 300
DEFAULT_ON_ERROR = "accept"
TIMEOUT_MIN_MS = 50
TIMEOUT_MAX_MS = 2000


@dataclass(frozen=True)
class Settings:
    url: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_MS / 1000.0
    on_error: str = DEFAULT_ON_ERROR

    @property
    def enabled(self) -> bool:
        return bool(self.url)


_cached: Settings | None = None


def parse_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = {} if env is None else env
    url = str(env.get("WAKE_VERIFY_URL", "") or "").strip()
    raw = str(env.get("WAKE_VERIFY_TIMEOUT_MS", "") or "").strip()
    if not raw:
        timeout_ms = DEFAULT_TIMEOUT_MS
    else:
        try:
            timeout_ms = int(raw)
        except ValueError:
            timeout_ms = DEFAULT_TIMEOUT_MS
    timeout_ms = min(TIMEOUT_MAX_MS, max(TIMEOUT_MIN_MS, timeout_ms))
    on_error = str(env.get("WAKE_VERIFY_ON_ERROR", DEFAULT_ON_ERROR) or "").strip().lower()
    if on_error not in ("accept", "reject"):
        on_error = DEFAULT_ON_ERROR
    return Settings(url=url, timeout_s=timeout_ms / 1000.0, on_error=on_error)


def settings() -> Settings:
    """Process settings, read once. Tests pass ``parse_settings`` around this."""
    global _cached
    if _cached is None:
        _cached = parse_settings(os.environ)
    return _cached


def classify(has_session: bool, streaming: bool | None) -> str:
    """``private`` has no pre-roll. ``stream`` is controller scoring or shadow."""
    if has_session or streaming is False:
        return "private"
    if streaming is True:
        return "stream"
    return "unknown"


def verdict(body, *, on_error: str) -> bool:
    """True accepts the wake. A missing or non-bool ``accept`` is an error."""
    accept = body.get("accept") if isinstance(body, dict) else None
    if isinstance(accept, bool):
        return accept
    return on_error != "reject"


def _fallback(cfg: Settings, device_id: str, reason: str) -> bool:
    accept = cfg.on_error != "reject"
    log.info("wake verify %s %s — %s", device_id, reason,
             "accept" if accept else "reject")
    return accept


async def post_json(url: str, payload: dict, timeout_s: float) -> tuple[int, dict | None]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = None
            return resp.status, body if isinstance(body, dict) else None


async def check(cfg: Settings, *, device_id: str, score, rms, pcm: bytes,
                post=None) -> bool:
    """POST one clip. Empty URL accepts without a request."""
    if not cfg.enabled:
        return True
    post = post_json if post is None else post
    wav = em_wake_samples.encode_wav(pcm) if pcm else b""
    payload = {
        "device_id": device_id,
        "score": None if score is None else round(float(score), 4),
        "rms": None if rms is None else round(float(rms), 6),
        "sample_rate": em_wake_samples.SAMPLE_RATE,
        "audio_wav_b64": base64.b64encode(wav).decode("ascii") if wav else "",
    }
    try:
        status, body = await post(cfg.url, payload, cfg.timeout_s)
    except asyncio.TimeoutError:
        return _fallback(cfg, device_id, "timeout")
    except aiohttp.ClientError:
        return _fallback(cfg, device_id, "error")
    except Exception:
        log.exception("wake verify error")
        return _fallback(cfg, device_id, "error")
    if status >= 400:
        return _fallback(cfg, device_id, f"http {status}")
    accepted = verdict(body, on_error=cfg.on_error)
    if not isinstance(body, dict) or not isinstance(body.get("accept"), bool):
        return _fallback(cfg, device_id, "bad response")
    log.info("wake verify %s — %s", device_id, "accept" if accepted else "reject")
    return accepted


def begin(*, device_id: str, score, rms, pcm: bytes, streaming: bool | None,
          has_session: bool = False, cfg: Settings | None = None,
          post=None) -> asyncio.Task | None:
    """Start the check, or return None when this wake must not be sent."""
    cfg = settings() if cfg is None else cfg
    if not cfg.enabled or classify(has_session, streaming) != "stream":
        return None
    return asyncio.create_task(check(
        cfg, device_id=device_id, score=score, rms=rms, pcm=pcm, post=post))


def cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done():
        task.cancel()


async def finish(task: asyncio.Task | None) -> bool:
    """Wait for ``begin``. No task means the check is off or does not apply."""
    if task is None:
        return True
    try:
        return bool(await task)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("wake verify failed")
        return settings().on_error != "reject"
