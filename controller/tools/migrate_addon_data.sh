#!/bin/sh
# migrate_addon_data.sh — copy paired-Dot data into the local add-on's directory.
#
# Supervisor names an add-on <repository>_<slug from config.yaml>. A folder
# in the local add-ons directory is repository "local", so this build is
# local_controller and its files are addons/data/local_controller. A git
# store's repository id is the first 8 hex characters of SHA-1 of the
# repository URL, lowercased (supervisor/store/utils.py,
# get_hash_from_repository). https://github.com/wilbowes/EchoMuse is
# acc06409, so the installed add-on is acc06409_controller. Those two
# directories are not the same one. Starting local_controller while its
# directory is empty creates a new database and a new device-link CA, and
# with device approval set to strict every Dot comes up unpaired.
#
# Run this on the Home Assistant host, not inside the add-on container.
# The container only sees its own /data. Stop the original add-on first.
# This copies. It does not move, and it does not delete the original.
#
#   tools/migrate_addon_data.sh copy \
#       --data-root /mnt/data/supervisor/addons/data
#   tools/migrate_addon_data.sh check \
#       --data-root /mnt/data/supervisor/addons/data
#   tools/migrate_addon_data.sh rollback \
#       --data-root /mnt/data/supervisor/addons/data
#
# Home Assistant OS keeps that root at /mnt/data/supervisor/addons/data.
# A Supervised install keeps it at /usr/share/hassio/addons/data. The
# Samba "addons" share is the local add-on source, not this data root.
#
# `prefix <url>` prints the repository id Supervisor would use. A fork
# URL is a different id. Adding ".git" changes it too.
set -eu

FROM_SLUG="acc06409_controller"
TO_SLUG="local_controller"
ROOT=""
MODE=""
REPLACE=0

usage() {
    echo "usage: migrate_addon_data.sh copy|check|rollback --data-root DIR [--from SLUG] [--to SLUG] [--replace]" >&2
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

[ -n "$ROOT" ] || usage
ROOT="${ROOT%/}"
SRC="$ROOT/$FROM_SLUG"
DST="$ROOT/$TO_SLUG"

# A replacement that starts without these creates a new CA. The four names
# are what em_pki.ensure_pki requires before it will reuse a CA.
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
    printf '%s' "$missing"
}

refuse_start() {
    echo "refusing to start: $DST is empty or incomplete ($1)." >&2
    echo "Copy $SRC (echomuse.db and tls/) into it, then run: $0 check --data-root $ROOT" >&2
    echo "Starting local_controller now would mint a new CA and unpair every Dot." >&2
    exit 1
}

case "$MODE" in
    check)
        missing="$(data_missing "$DST")"
        if [ -n "$missing" ]; then
            refuse_start "$missing"
        fi
        echo "$DST has echomuse.db and tls/. Safe to start $TO_SLUG."
        ;;
    copy)
        missing="$(data_missing "$SRC")"
        if [ -n "$missing" ]; then
            echo "source $SRC is missing:$missing" >&2
            echo "refusing to start: nothing was copied." >&2
            exit 1
        fi
        if [ "$SRC" = "$DST" ]; then
            echo "source and target are the same directory" >&2
            exit 1
        fi
        if [ -s "$DST/echomuse.db" ] && [ "$REPLACE" != 1 ]; then
            echo "$DST already has echomuse.db. Stop the local add-on and re-run with --replace to overwrite it." >&2
            exit 1
        fi
        mkdir -p "$DST/tls"
        cp -p "$SRC/echomuse.db" "$DST/echomuse.db"
        for extra in echomuse.db-wal echomuse.db-shm; do
            if [ -f "$SRC/$extra" ]; then
                cp -p "$SRC/$extra" "$DST/$extra"
            else
                rm -f "$DST/$extra"
            fi
        done
        for name in $tls_names; do
            cp -p "$SRC/tls/$name" "$DST/tls/$name"
        done
        missing="$(data_missing "$DST")"
        if [ -n "$missing" ]; then
            refuse_start "$missing"
        fi
        echo "Copied echomuse.db and tls/ from $SRC to $DST."
        echo "The original is unchanged. Start $TO_SLUG only after this check passes."
        ;;
    rollback)
        missing="$(data_missing "$SRC")"
        if [ -n "$missing" ]; then
            echo "original $SRC is missing:$missing" >&2
            echo "refusing rollback: the original data directory is not intact." >&2
            exit 1
        fi
        echo "Original data is still at $SRC (this command does not move it)."
        echo "Stop $TO_SLUG, then start $FROM_SLUG. Do not uninstall either add-on."
        ;;
    *)
        usage
        ;;
esac
