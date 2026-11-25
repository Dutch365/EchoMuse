"""Volume scale conversion between the device's native level and HA's float.

Split out as pure logic for the reason em_linkauth.py was: the three call
sites (em_controller, em_esphome, em_api) each had their OWN copy of
`level / 175`, so the scale lived in three places and none of them were
covered by a test. Changing the ceiling meant finding all three.

The device level is the raw tinymix ctl 61 index — the tlv320aic32x4 DAC
digital volume, 0.5dB per step with 0dB (unity) at index 127. The scale is
therefore dB-linear, which is roughly perceptually linear, so HA's 0.0–1.0
maps onto it in proportion, needing no taper — but onto the BUTTON band, not
the whole control: 0 is silent, 1% is the band's floor (VOLUME_FLOOR, -35dB,
just audible) and 100% unity. Mapped over all of 0..127, HA's bottom third
was near-silent. Every percentage lands on one of the 36 levels the buttons
step through, so the ring shows the same arc as the presses that reach it.
The device's PercentToLevel (device/internal/server/volume.go) is the same
law, for Sendspin; tests/test_volume.py pins the two together.

DEVICE_VOLUME_MAX is 127 and NOT the control's own maximum of 175. Indexes
above 127 apply positive digital gain to near-full-scale PCM and saturate
inside the DAC — measured at 65% THD by index 153 (see
device/internal/server/volume.go for the full measurement and why stock
FireOS never touches this control).
"""

import logging
import math
import os

log = logging.getLogger("echomuse")

# Codec unity gain. The mixer control accepts up to 175; everything above
# this clips. See the module docstring.
DEVICE_VOLUME_MAX = 127

# 0.5dB per index step, 0dB at DEVICE_VOLUME_MAX.
DB_PER_STEP = 0.5

# The button band: floor to unity in steps of VOLUME_STEP (1dB), 36 levels,
# one per state of the ring. volumeButtonFloor and volumeStep in
# device/internal/server/volume.go; pinned by tests/test_volume.py.
VOLUME_FLOOR = 57
VOLUME_STEP = 2
VOLUME_STEPS = (DEVICE_VOLUME_MAX - VOLUME_FLOOR) // VOLUME_STEP  # 35


def level_to_db(level: int) -> float:
    """Device level as dB relative to unity. Index 127 -> 0.0, index 0 -> -63.5."""
    return (level - DEVICE_VOLUME_MAX) * DB_PER_STEP


def _stock_device_level_to_ha(level: int) -> float:
    """Convert a device volume level to an HA float (0.0–1.0): 0 for silent,
    0.01 for anything audible at or below the floor (0 would read as muted),
    else the level's place in the band."""
    try:
        level = float(level)
    except (TypeError, ValueError):
        return 0.0
    if level <= 0:
        return 0.0
    if level <= VOLUME_FLOOR:
        return 0.01
    level = min(level, DEVICE_VOLUME_MAX)
    return (1 + (level - VOLUME_FLOOR) / (DEVICE_VOLUME_MAX - VOLUME_FLOOR) * 99) / 100


def _stock_ha_volume_to_device(volume: float) -> int:
    """Convert an HA volume float (0.0–1.0) to a device volume level.

    Clamped to the codec's unity gain, so HA asking for full volume can never
    put the DAC into positive digital gain. Rounds half away from zero, as the
    device's Go `math.Round` does — Python's round() rounds half to even.
    """
    pct = float(volume) * 100
    if pct <= 0:
        return 0
    if pct >= 100:
        return DEVICE_VOLUME_MAX
    n = max(0, math.floor((pct - 1) / 99 * VOLUME_STEPS + 0.5))
    return VOLUME_FLOOR + n * VOLUME_STEP


# ─── House (hv-084): which law HA percentages use ─────────────────────────────
#
# Upstream 2.27 maps HA's percentage onto the 36-level button band above
# (1% = -35dB, 100% = unity). The house controller before hv-084 mapped it in
# proportion over the whole control, round(v * 127), and the house automations
# (night 35%, announcements 75%) are tuned to that: under the band law 35% is
# +18.5dB louder and 75% +7dB. One controller drives every Dot, so the law is
# an add-on option (`ha_volume_law`, env HA_VOLUME_LAW): "legacy" (default)
# keeps the proportional law everywhere the controller converts an HA/API
# percentage; "stock" is upstream's band. Button steps and Sendspin's
# PercentToLevel are firmware-side and use the band either way.
#
# Read here, not swapped as functions: em_controller binds aliases to
# ha_volume_to_device / device_level_to_ha at import.

LAW_LEGACY = "legacy"
LAW_STOCK = "stock"
LAWS = (LAW_LEGACY, LAW_STOCK)


def _law_from_env() -> str:
    raw = (os.environ.get("HA_VOLUME_LAW") or LAW_LEGACY).strip().lower()
    if raw not in LAWS:
        log.warning(f"[volume] HA_VOLUME_LAW={raw!r} is not one of {LAWS}; using legacy")
        return LAW_LEGACY
    return raw


LAW = _law_from_env()


def _legacy_device_level_to_ha(level: int) -> float:
    """House law before hv-084: level / 127, clamped to 0.0–1.0."""
    try:
        return max(0.0, min(1.0, float(level) / DEVICE_VOLUME_MAX))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _legacy_ha_volume_to_device(volume: float) -> int:
    """House law before hv-084: round(v * 127), clamped to 0..127 (unity)."""
    return max(0, min(DEVICE_VOLUME_MAX, round(float(volume) * DEVICE_VOLUME_MAX)))


def device_level_to_ha(level: int) -> float:
    """Device level -> HA float (0.0–1.0) under the configured law."""
    if LAW == LAW_STOCK:
        return _stock_device_level_to_ha(level)
    return _legacy_device_level_to_ha(level)


def ha_volume_to_device(volume: float) -> int:
    """HA float (0.0–1.0) -> device level under the configured law. Never
    above unity (127) under either."""
    if LAW == LAW_STOCK:
        return _stock_ha_volume_to_device(volume)
    return _legacy_ha_volume_to_device(volume)


# ─── The stock-sound profile's volume law (#229) ──────────────────────────────
#
# The plain law (0.5dB per level) lifted by STOCK_CEILING_DB, so full volume
# drives the chain's compressors and limiter. Stock FireOS's own table was
# tried first and dropped: it is front-loaded (most of its range is gone by
# 55%), and its +14.5dB above 70% bought little loudness, measured through
# our chain: past about +10dB, music gains under 0.5dB per dB of input and
# the rest is compression. Keeping 0.5dB per level also keeps every physical
# button press the same size and leaves HA, Music Assistant and stored startupVolume
# values meaning what they always have. The ceiling was picked by ear.
STOCK_CEILING_DB = 10.0


def stock_level_db(level: int) -> float | None:
    """Gain in dB for a device level under the stock-sound profile, or None
    for silence (level 0)."""
    if level <= 0:
        return None
    return level_to_db(min(level, DEVICE_VOLUME_MAX)) + STOCK_CEILING_DB
