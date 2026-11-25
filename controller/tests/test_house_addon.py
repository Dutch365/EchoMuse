"""
hv-084: the house add-on keeps every option fork main had before the
catch-up, and the controller build stays the local add-on.

Supervisor drops an option the schema no longer lists, so a key lost in an
upstream merge silently resets that setting on the Dell; losing
`internal_install_secret` breaks leo_jobs' nightly wakeword_install. The
wiring of each key (options, schema, translation, em_start) is
test_deploy.test_addon_options_are_wired_end_to_end; every em_*.py being
COPYed is test_deploy.test_dockerfile_copies_every_controller_module.
"""

from pathlib import Path

import yaml

CONTROLLER = Path(__file__).resolve().parents[1]

# fork main c285c3d (add-on 2.26.13), before hv-084.
FORK_MAIN_OPTIONS = {
    'adaptive_endpoint', 'adaptive_no_speech_ms', 'adaptive_vad_step',
    'arb_score_mode', 'arbitration_cap_ms', 'arbitration_early_min_snr_db',
    'arbitration_early_ms', 'arbitration_early_snr_gate', 'arbitration_hold_ms',
    'arbitration_min_snr_db', 'arbitration_min_wait_ms', 'arbitration_mode',
    'arbitration_room_offsets_db', 'arbitration_takeover',
    'arbitration_takeover_margin_db', 'arbitration_takeover_max_ms',
    'cue_address', 'cue_min_chars', 'cue_min_words', 'cue_wake_score', 'debug',
    'device_approval', 'echo_handoff_margin_db', 'echo_margin_db',
    'echo_override_db', 'echo_solo_min_score', 'esphome_port_base',
    'esphome_project_version', 'external_audio_faint_dbfs',
    'external_audio_gate', 'extra_ca_cert', 'ha_wake_cooldown',
    'ha_wake_cooldown_s', 'internal_install_secret', 'listen_confidence',
    'log_levels', 'loop_watchdog', 'loser_cooldown_mode', 'loser_cooldown_s',
    'loser_override_wake', 'max_listen_ms', 'mdns_name', 'no_reply_cue',
    'oww_model', 'oww_threshold', 'playback_tail_ms', 'reply_fallback',
    'require_device_tls', 'room_media_players', 'server_host', 'server_ip',
    'speech_gate_threshold', 'trail_cutoff_ms', 'tts_wait_timeout',
    'wake_verify_on_error', 'wake_verify_timeout_ms', 'wake_verify_url',
}


def _config():
    return yaml.safe_load((CONTROLLER / "config.yaml").read_text())


def test_every_fork_main_option_survives():
    cfg = _config()
    for sec in ("options", "schema"):
        lost = sorted(FORK_MAIN_OPTIONS - set(cfg[sec]))
        assert not lost, f"{sec} lost house keys: {lost}"


def test_house_version_and_new_keys():
    cfg = _config()
    assert cfg["version"] == "2.27.1-hv084"
    assert cfg["options"]["ha_volume_law"] == "legacy"
    assert cfg["schema"]["ha_volume_law"] == "list(legacy|stock)"
    assert cfg["options"]["internal_install_secret"] == ""
    assert cfg["schema"]["internal_install_secret"] == "password?"
    assert "## 2.27.1-hv084" in (CONTROLLER / "CHANGELOG.md").read_text()


def test_share_is_mapped_writable():
    """wake-arbitration.jsonl is written under /share/leo/logs."""
    maps = _config()["map"]
    share = [m for m in maps if (m.get("type") if isinstance(m, dict) else m) == "share"]
    assert share, "share is not mapped"
    assert all(not (isinstance(m, dict) and m.get("read_only", False)) for m in share)


def test_websockets_pin_is_not_lowered():
    req = (CONTROLLER / "requirements.txt").read_text()
    line = next(l for l in req.splitlines() if l.startswith("websockets"))
    ver = tuple(int(x) for x in line.split("==")[1].split("."))
    assert ver >= (17, 4), line
