#!/bin/sh
# migrate_addon_data.sh — copy paired-Dot data into the local app's directory.
#
# Supervisor names an app <repository>_<slug from config.yaml>. A folder in
# the local apps directory is repository "local", so this build is
# local_controller. A git store's repository id is the first 8 hex characters
# of SHA-1 of the repository URL, lowercased (supervisor/store/utils.py,
# get_hash_from_repository). https://github.com/wilbowes/EchoMuse is
# acc06409, so the installed app is acc06409_controller.
#
# This Supervisor keeps data at /mnt/data/supervisor/apps/data and the local
# app source at apps/local. Older builds use addons/data and addons/local.
# The script uses apps/ when that directory is present, otherwise addons/.
# --data-root overrides the detection.
#
# The copy is the whole data directory. Device config points at
# /data/oww_models/hey_leo_v6.onnx, and firmware/ is read from /data too.
# A copy of only echomuse.db and tls/ leaves those paths missing.
#
# copy sets the original app's boot to manual, via the Supervisor API
# POST /apps/<slug>/options {"boot":"manual"}, so a reboot does not start
# it on the same ports as the local app. rollback sets that boot back to
# auto. Run this where SUPERVISOR_TOKEN is set (the Advanced SSH app).
# Tests set HA_CLI to a stand-in for that call.
#
#   tools/migrate_addon_data.sh copy
#   tools/migrate_addon_data.sh check
#   tools/migrate_addon_data.sh rollback
#   tools/migrate_addon_data.sh prefix https://github.com/wilbowes/EchoMuse
set -eu

FROM_SLUG="acc06409_controller"
TO_SLUG="local_controller"
ROOT=""
MODE=""
REPLACE=0
WAKE_MODEL="oww_models/hey_leo_v6.onnx"

usage() {
    echo "usage: migrate_addon_data.sh copy|check|rollback [--data-root DIR] [--from SLUG] [--to SLUG] [--replace]" >&2
    echo "       migrate_addon_data.sh prefix URL" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        copy|check|rollback|prefix) MODE="$1" ;;
        --data-root)
            [ $# -ge 2 ] || usage
            ROOT="$2"
            shift
            ;;
        --from)
            [ $# -ge 2 ] || usage
            FROM_SLUG="$2"
            shift
            ;;
        --to)
            [ $# -ge 2 ] || usage
            TO_SLUG="$2"
            shift
            ;;
        --replace) REPLACE=1 ;;
        *)
            if [ "$MODE" = "prefix" ] && [ -z "$ROOT" ]; then
                ROOT="$1"
            else
                usage
            fi
            ;;
    esac
    shift
done

[ -n "$MODE" ] || usage

prefix_of() {
    # Supervisor: hashlib.sha1(url.lower().encode()).hexdigest()[:8]
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sha1sum | cut -c1-8
}

if [ "$MODE" = "prefix" ]; then
    [ -n "$ROOT" ] || usage
    prefix_of "$ROOT"
    exit 0
fi

# apps/ is the current Supervisor layout. addons/ is the previous name.
# Prefer a directory that already holds the installed app. SUPERVISOR_BASE
# replaces the host prefixes so a test can point at a fixture tree.
detect_root() {
    if [ -n "$ROOT" ]; then
        ROOT="${ROOT%/}"
        return 0
    fi
    bases="/mnt/data/supervisor /usr/share/hassio"
    if [ -n "${SUPERVISOR_BASE:-}" ]; then
        bases="$SUPERVISOR_BASE"
    fi
    for base in $bases; do
        for kind in apps addons; do
            dir="$base/$kind/data"
            if [ -d "$dir/$FROM_SLUG" ]; then
                ROOT="$dir"
                return 0
            fi
        done
    done
    for base in $bases; do
        if [ -d "$base/apps/data" ]; then
            ROOT="$base/apps/data"
            return 0
        fi
        if [ -d "$base/addons/data" ]; then
            ROOT="$base/addons/data"
            return 0
        fi
    done
    echo "Could not find a Supervisor data directory (apps/data, then addons/data) under: $bases" >&2
    echo "Pass --data-root DIR to set it." >&2
    exit 2
}

detect_root
SRC="$ROOT/$FROM_SLUG"
DST="$ROOT/$TO_SLUG"

# The four tls names are what em_pki.ensure_pki requires before it reuses a CA.
tls_names="ca.pem ca.key server.pem server.key"

data_missing() {
    dir="$1"
    missing=""
    if [ ! -s "$dir/echomuse.db" ]; then
        missing="$missing echomuse.db"
    fi
    for name in $tls_names; do
        if [ ! -s "$dir/tls/$name" ]; then
            missing="$missing tls/$name"
        fi
    done
    if [ ! -s "$dir/$WAKE_MODEL" ]; then
        missing="$missing $WAKE_MODEL"
    fi
    if [ ! -d "$dir/firmware" ] || [ -z "$(ls -A "$dir/firmware" 2>/dev/null)" ]; then
        missing="$missing firmware/"
    fi
    printf '%s' "$missing"
}

refuse_start() {
    echo "refusing to start: $DST is empty or incomplete ($1)." >&2
    echo "Copy the whole of $SRC into it, then run: $0 check --data-root $ROOT" >&2
    echo "Starting local_controller now would mint a new CA, or leave Dots pointing at a missing wake model." >&2
    exit 1
}

# POST {"boot":"manual"|"auto"} only changes that field. The handler
# applies keys that are present (supervisor/api/apps.py SCHEMA_OPTIONS).
# HA_CLI is a test stand-in. On the host, SUPERVISOR_TOKEN and curl reach
# the same API. /apps/ is current; /addons/ is the same handler.
set_boot() {
    slug="$1"
    mode="$2"
    if [ -n "${HA_CLI:-}" ]; then
        if ! "$HA_CLI" apps options "$slug" --boot "$mode"; then
            echo "refusing to continue: could not set $slug boot to $mode" >&2
            exit 1
        fi
        echo "Set $slug boot to $mode."
        return 0
    fi
    if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
        echo "refusing to continue: SUPERVISOR_TOKEN is unset, so $slug boot cannot be set to $mode." >&2
        echo "Run this from the Advanced SSH app, or POST {\"boot\":\"$mode\"} to /apps/$slug/options." >&2
        exit 1
    fi
    if ! command -v curl >/dev/null 2>&1; then
        echo "refusing to continue: curl is not available to set $slug boot to $mode." >&2
        exit 1
    fi
    code=""
    for prefix in apps addons; do
        code="$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
            -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"boot\":\"$mode\"}" \
            "http://supervisor/$prefix/$slug/options" || true)"
        if [ "$code" = "200" ]; then
            echo "Set $slug boot to $mode via /apps-or-addons ($prefix)."
            return 0
        fi
    done
    echo "refusing to continue: setting $slug boot to $mode failed (HTTP ${code:-none})." >&2
    exit 1
}

case "$MODE" in
    check)
        missing="$(data_missing "$DST")"
        if [ -n "$missing" ]; then
            refuse_start "$missing"
        fi
        echo "Data root: $ROOT"
        echo "$DST has the database, tls/, $WAKE_MODEL and firmware/. Safe to start $TO_SLUG."
        ;;
    copy)
        missing="$(data_missing "$SRC")"
        if [ -n "$missing" ]; then
            echo "source $SRC is missing:$missing" >&2
            echo "refusing to start: nothing was copied, and boot was not changed." >&2
            exit 1
        fi
        if [ "$SRC" = "$DST" ]; then
            echo "source and target are the same directory" >&2
            exit 1
        fi
        if [ -s "$DST/echomuse.db" ] && [ "$REPLACE" != 1 ]; then
            echo "$DST already has echomuse.db. Stop the local app and re-run with --replace to overwrite it." >&2
            exit 1
        fi
        # Before the copy, so a reboot mid-copy does not start the original
        # on these ports. The original directory is not moved.
        set_boot "$FROM_SLUG" manual
        if [ "$REPLACE" = 1 ]; then
            rm -rf "$DST"
        fi
        mkdir -p "$DST"
        cp -a "$SRC"/. "$DST"/
        missing="$(data_missing "$DST")"
        if [ -n "$missing" ]; then
            refuse_start "$missing"
        fi
        echo "Data root: $ROOT"
        echo "Local app source: $(dirname "$ROOT")/local"
        echo "Copied $SRC onto $DST (the whole directory). Original boot is manual."
        echo "The original files are unchanged. Start $TO_SLUG only after check passes."
        ;;
    rollback)
        missing="$(data_missing "$SRC")"
        if [ -n "$missing" ]; then
            echo "original $SRC is missing:$missing" >&2
            echo "refusing rollback: the original data directory is not intact, and boot was not changed." >&2
            exit 1
        fi
        set_boot "$FROM_SLUG" auto
        echo "Data root: $ROOT"
        echo "Original data is still at $SRC. Its boot is auto again."
        echo "Stop $TO_SLUG, then start $FROM_SLUG. Do not uninstall either app."
        ;;
    *)
        usage
        ;;
esac
