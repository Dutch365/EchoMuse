"""
em_start.py — Home Assistant add-on entrypoint

When running as a Home Assistant Supervisor add-on, options set in the add-on
UI land in /data/options.json rather than the environment. This translates
each option into the env var em_controller.py already reads (see
.env.example) and then execs into it directly, so em_controller.py itself
stays completely unaware of Home Assistant.

Outside the add-on (docker-compose, bare `python em_controller.py`),
/data/options.json doesn't exist and this is a no-op passthrough. Any env var
already set explicitly is left alone — options.json only fills in what's
missing.
"""

import json
import os
import sys
from pathlib import Path

import em_cacerts

OPTIONS_PATH = Path("/data/options.json")

# What em_pki.ensure_pki requires before it reuses a CA. Missing any of
# these, the controller generates a new one and every paired Dot fails TLS.
TLS_FILES = ("ca.pem", "ca.key", "server.pem", "server.key")
# Device config stores this path. A copy that brings the database and not
# the file leaves every Dot pointing at a model that is not there.
WAKE_MODEL = Path("oww_models") / "hey_leo_v6.onnx"

# config.yaml's `options` keys map to env vars by uppercasing — kept as an
# explicit table rather than a blanket `.upper()`, so a renamed or removed
# add-on option is caught here rather than silently reaching em_controller.py
# under a name it does not read. options.json is generated from config.yaml's
# own schema, so a key missing from this table means those two files have
# drifted — a packaging bug, and one whose only other symptom is a setting
# that quietly does nothing.
OPTION_ENV_VARS = {
    "server_host": "SERVER_HOST",
    "server_ip": "SERVER_IP",
    "mdns_name": "MDNS_NAME",
    "esphome_project_version": "ESPHOME_PROJECT_VERSION",
    "esphome_port_base": "EM_ESPHOME_PORT_BASE",
    "oww_model": "OWW_MODEL",
    "oww_threshold": "OWW_THRESHOLD",
    "require_device_tls": "REQUIRE_DEVICE_TLS",
    "device_approval": "DEVICE_APPROVAL",
    "debug": "DEBUG",
    "log_levels": "LOG_LEVELS",
    "extra_ca_cert": "EM_EXTRA_CA_CERT",
    "tts_wait_timeout": "EM_TTS_WAIT_TIMEOUT",
    "max_listen_ms": "EM_MAX_LISTEN_MS",
    "trail_cutoff_ms": "EM_TRAIL_CUTOFF_MS",
    "listen_confidence": "EM_LISTEN_CONFIDENCE",
    "speech_gate_threshold": "EM_SPEECH_GATE_THRESHOLD",
    "loop_watchdog": "EM_LOOP_WATCHDOG",
    "arbitration_early_ms": "EM_ARBITRATION_EARLY_MS",
    "no_reply_cue": "EM_NO_REPLY_CUE",
    "cue_wake_score": "EM_CUE_WAKE_SCORE",
    "cue_min_words": "EM_CUE_MIN_WORDS",
    "cue_min_chars": "EM_CUE_MIN_CHARS",
    "cue_address": "EM_CUE_ADDRESS",
    "reply_fallback": "EM_REPLY_FALLBACK",
    "wake_verify_url": "WAKE_VERIFY_URL",
    "wake_verify_timeout_ms": "WAKE_VERIFY_TIMEOUT_MS",
    "wake_verify_on_error": "WAKE_VERIFY_ON_ERROR",
}

def apply_options(options: dict) -> None:
    for key, value in options.items():
        env_key = OPTION_ENV_VARS.get(key)
        if env_key is None:
            # Warn rather than exit: the add-on failing to boot over one
            # stray key would be a worse outcome than the setting being
            # ignored, and this line names it in the add-on log either way.
            print(f"em_start: no env var mapped for add-on option {key!r} —"
                  " config.yaml and OPTION_ENV_VARS have drifted", flush=True)
            continue
        if value == "":
            # An unfilled required field (server_ip has no sane default) —
            # leave it unset so em_controller.py's own fallback applies
            # rather than handing it a literal empty string.
            continue
        if isinstance(value, bool):
            # em_controller.py checks `== "1"` (see REQUIRE_DEVICE_TLS in
            # .env.example) — Python's str(bool) would produce "True"/"False"
            # and silently leave every bool option permanently off.
            env_value = "1" if value else "0"
        else:
            env_value = str(value)
        os.environ.setdefault(env_key, env_value)


def missing_existing_data(db_path: str) -> list[str]:
    """Files a replacement add-on must already have. Empty means none missing."""
    missing = []
    database = Path(db_path)
    if not database.is_file() or database.stat().st_size == 0:
        missing.append(str(database))
    root = database.parent
    tls = Path(os.environ.get("TLS_DIR") or (root / "tls"))
    for name in TLS_FILES:
        path = tls / name
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(str(path))
    model = root / WAKE_MODEL
    if not model.is_file() or model.stat().st_size == 0:
        missing.append(str(model))
    firmware = root / "firmware"
    if not firmware.is_dir() or not any(firmware.iterdir()):
        missing.append(str(firmware))
    return missing


def ensure_existing_data() -> None:
    """Refuse to start a replacement whose /data was not copied in.

    Set only on the local add-on produced by ``make_dev_addon.sh --keep-slug``.
    A local app's data directory is ``apps/data/local_controller`` (older
    Supervisor builds use ``addons/data``), which Supervisor creates empty.
    Starting then mints a database and a CA, and with device approval left
    on strict every Dot comes up unpaired. The wake model and firmware have
    to be in that directory too: device config points at them.
    """
    if os.environ.get("EM_REQUIRE_EXISTING_DATA") != "1":
        return
    db_path = os.environ.get("DB_PATH", "/data/echomuse.db")
    missing = missing_existing_data(db_path)
    if not missing:
        return
    print("em_start: refusing to start — copied data is missing:", flush=True)
    for path in missing:
        print(f"em_start:   {path}", flush=True)
    print("em_start: copy the installed app's whole data directory "
          "(database, tls/, oww_models/, firmware/) and start again. "
          "tools/migrate_addon_data.sh does that copy and the same check.",
          flush=True)
    sys.exit(1)


def main() -> None:
    if OPTIONS_PATH.is_file():
        apply_options(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
    ensure_existing_data()
    # Trust a private CA before starting, so BOTH TLS stacks see it — Python for
    # the TTS fetch and GnuTLS-linked ffmpeg for media URLs. Done here rather than
    # in em_controller because it is a property of the container, has to happen
    # before anything opens a connection, and needs the root privileges this
    # process still has.
    #
    # Failing hard is deliberate. The alternative is a controller that starts
    # cleanly and then dies on every voice turn with a certificate error nobody
    # connects back to this setting — the user set an option and it silently did
    # not take.
    extra = os.environ.get("EM_EXTRA_CA_CERT", "").strip()
    if extra:
        try:
            print(f"em_start: {em_cacerts.install(extra)}", flush=True)
        except em_cacerts.CATrustError as err:
            print(f"em_start: EM_EXTRA_CA_CERT — {err}", flush=True)
            sys.exit(1)

    os.execvp("python3", ["python3", "-u", "em_controller.py"])


if __name__ == "__main__":
    main()
