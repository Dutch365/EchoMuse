"""Hold the listening ring until the wake check has answered (home-voice-hub #171).

Since #263 an Echo draws its listening ring at its own wake crossing, from
the ``listeningAnim`` spec the controller pushes it. That is before the
crossing is reported, so before the second-stage wake check
(``em_wakeverify``) has seen it: a wake the checker rejects still lights the
ring for the 0.3-0.4s it takes the controller to send ``leds_off``. The wake
sound already waits for the verdict on both paths; the ring was the leak.

``WAKE_RING_AFTER_VERIFY=1`` (add-on option ``wake_ring_after_verify``,
default off) closes it without a firmware change:

- The device is pushed ``listeningAnim: {}`` instead of the real spec.
  Firmware draws only a spec with a pattern, so it draws nothing at the
  crossing. The key is sent, never omitted: the config push is partial, and
  an absent key would leave the old spec cached on the device.
- The ring then comes from ``leds_listening`` at turn start, which runs only
  after the verdict (or the on-error accept) and after arbitration. Losers
  and rejected wakes stay dark.
- A barge-in has no checker and happens mid-reply, where the ring is the only
  sign the Echo heard. The controller draws it as soon as the barge is
  taken, the step the device used to do itself.
- The no-HA stand-down held a ring the device had lit. With the ring dark,
  the controller lights it once the verdict is in, or plays nothing if the
  checker rejected the wake (no bare orange flash).

Off, or with no ``WAKE_VERIFY_URL``, every function here does what the
controller did before. The option is read once at start; an add-on option
change restarts the controller, every Echo re-registers, and gets the real
spec back.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping

import em_wakeverify

log = logging.getLogger("echomuse.wakering")

ENV = "WAKE_RING_AFTER_VERIFY"

# The pushed spec when the device must not draw. Firmware skips a spec whose
# pattern is empty (device/cmd/server.go onWakeCrossing).
NO_LOCAL_RING: dict = {}


def parse(env: Mapping[str, str] | None = None) -> bool:
    """On only when asked for AND a checker is configured."""
    env = {} if env is None else env
    raw = str(env.get(ENV, "") or "").strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        return False
    return em_wakeverify.parse_settings(env).enabled


_cached: bool | None = None


def enabled() -> bool:
    """Process setting, read once. Tests set ``_cached`` around this."""
    global _cached
    if _cached is None:
        _cached = parse(os.environ)
        if _cached:
            log.info("wake ring waits for the wake check (%s)", ENV)
    return _cached


def listening_anim_message(device) -> dict | None:
    """The config push that sets the device's cached listening ring.

    None when nothing is to be sent: firmware without ``led_anim`` never drew
    locally, and with the option off a scene with no listening animation has
    nothing to hand over.
    """
    if not getattr(device, "led_anim_capable", False):
        return None
    if enabled():
        return {"type": "config", "listeningAnim": dict(NO_LOCAL_RING)}
    anim = (getattr(device, "led_scene", None) or {}).get("listening_anim")
    if not anim:
        return None
    return {"type": "config", "listeningAnim": anim}


async def push_listening_anim(device) -> None:
    """#263: hand the device the ring to draw at its own crossing.

    At registration this is what lets a wake the device detected ITSELF light
    the ring immediately, instead of waiting for leds_listening to make the
    round trip (measured +522ms before the ring moved; the control-plane tail
    reaches 2s, #139). On a live scene change it keeps the cached spec in
    step; without it the ring lit locally in the OLD scene's colours until
    the next reconnect. With the option on, both send the empty spec.
    """
    msg = listening_anim_message(device)
    if msg is not None:
        await device.send_control(msg)


async def barge_ring(device, light: Callable[[object], Awaitable[None]]) -> None:
    """Draw the ring for a barge-in the device was told not to draw."""
    if enabled() and getattr(device, "led_anim_capable", False):
        await light(device)


async def before_no_ha_hold(device,
                            light: Callable[[object], Awaitable[None]]) -> bool:
    """Ready the ring the no-HA cue holds. False: play no cue at all.

    Off, the device lit the ring at its crossing and there is nothing to do.
    On, the ring is dark: wait for this wake's verdict, then light it, so the
    stand-down looks as it did (ring, then orange). A rejected wake was not a
    wake, and shows nothing.
    """
    if not enabled():
        return True
    task = em_wakeverify.current(getattr(device, "device_id", "") or "")
    if not await em_wakeverify.finish(task):
        return False
    await light(device)
    return True
