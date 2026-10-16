"""Link-stall lines carry the Dot's Wi-Fi RSSI and power-save state."""

from pathlib import Path

import _sources
import em_linklog
import em_state

CONTROLLER = Path(__file__).resolve().parents[1]


def test_suffix_says_unknown_until_a_stat_arrives():
    class Dev:
        stats = None
    assert em_linklog.suffix(Dev()) == " (wifi rssi=unknown power-save=unknown)"
    Dev.stats = {"wifiRssi": -62, "wifiPowerSave": "off"}
    dev = Dev()
    dev.stats = {"wifiRssi": -62, "wifiPowerSave": "off"}
    assert em_linklog.suffix(dev) == " (wifi rssi=-62dBm power-save=off)"


def test_suffix_for_reads_the_live_device():
    class Dev:
        stats = {"wifiRssi": -71, "wifiPowerSave": None}
    em_state.devices["dot-c"] = Dev()
    try:
        assert em_linklog.suffix_for("dot-c") == (
            " (wifi rssi=-71dBm power-save=unknown)")
        assert "unknown" in em_linklog.suffix_for("missing")
    finally:
        em_state.devices.pop("dot-c", None)


def test_rtt_and_media_stalls_include_the_suffix():
    pong = _sources.def_source("em_controller._on_pong")
    assert "em_linklog.suffix(device)" in pong
    stats = _sources.def_source("em_controller._on_stats")
    assert "wifiPowerSave" in stats
    player = (CONTROLLER / "em_player.py").read_text()
    assert "link stalled or dead" in player
    assert "em_linklog.suffix_for" in player
    monitor = _sources.def_source("event_loop_lag_monitor")
    assert "em_stallwatch.measure" in monitor
