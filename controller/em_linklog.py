"""Wi-Fi RSSI and power-save beside a link-stall log line.

The firmware reports wifiRssi on the stats tick. Power-save is read from
wifiPowerSave when a build sends it, and logged as unknown until then.
The suffix is appended to a line the caller was already going to write.
"""

from __future__ import annotations


def _fmt(stats) -> str:
    if not isinstance(stats, dict):
        stats = {}
    rssi = stats.get("wifiRssi")
    ps = stats.get("wifiPowerSave")
    rssi_s = "unknown" if rssi is None else f"{rssi}dBm"
    ps_s = "unknown" if ps is None else str(ps)
    return f" (wifi rssi={rssi_s} power-save={ps_s})"


def suffix(device) -> str:
    return _fmt(getattr(device, "stats", None))


def suffix_for(device_id: str) -> str:
    try:
        import em_state
        device = em_state.devices.get(device_id)
    except Exception:
        device = None
    if device is None:
        return _fmt({})
    return suffix(device)
