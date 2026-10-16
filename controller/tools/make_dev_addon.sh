#!/usr/bin/env sh
# make_dev_addon.sh — package the working tree as a LOCAL Home Assistant add-on.
#
# Supervisor clones the repository's DEFAULT BRANCH for a normal add-on, so a
# branch cannot be installed — which means add-on changes are untestable until
# they are on main, exactly backwards for anything add-on-specific (ingress,
# options, /data, auth). A local add-on is the way round it: Supervisor builds
# whatever sits in apps/local/<folder> on the Home Assistant host
# (older Supervisor builds use addons/local).
#
# This generates that folder from the real config.yaml rather than asking
# anyone to hand-edit it. Hand-editing is how the dev copy and the shipped one
# drift, and a divergence in exactly this file is what shipped a controller
# with no ingress support (#160).
#
# Usage, from controller/:
#     tools/make_dev_addon.sh [--keep-slug] [output-dir]
#
# --keep-slug leaves slug "controller". Supervisor still prefixes a local
# add-on, so the installed name is local_controller and its data directory
# is apps/data/local_controller (older Supervisor: addons/data). That is
# not the git app's directory (apps/data/acc06409_controller for the
# wilbowes store URL). The build sets EM_REQUIRE_EXISTING_DATA=1, and
# em_start refuses to start until the whole data directory has been copied
# in, including the wake model and firmware. tools/migrate_addon_data.sh
# does that copy and sets the original app's boot to manual. The default
# slug is controller-dev so a local build can
# sit beside the released add-on; that one has its own empty /data.
#
# Then copy the result to apps/local on the Home Assistant host (older
# Supervisor builds use addons/local; the Samba share, or scp via the SSH
# app) and look in
# Settings -> Add-ons -> Add-on Store; local add-ons appear at the top.
#
# Nothing here compiles: ffmpeg is apt, and onnxruntime, speexdsp-ns, scipy
# and scikit-learn are all prebuilt wheels. The build is downloads, so it
# costs minutes rather than the ordeal config.yaml's comment implies.
set -eu

SRC="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
KEEP_SLUG=0
OUT=""
for arg in "$@"; do
    case "$arg" in
        --keep-slug) KEEP_SLUG=1 ;;
        *) OUT="$arg" ;;
    esac
done
OUT="${OUT:-$SRC/../dist/echomuse-dev}"

[ -f "$SRC/config.yaml" ] || { echo "no config.yaml in $SRC" >&2; exit 1; }

rm -rf "$OUT"
mkdir -p "$OUT"

# The build context is the controller directory. Copy it wholesale minus the
# things that must not travel: a local database and TLS material would
# overwrite the add-on's own, and __pycache__ from a different Python is at
# best noise.
tar -cf - -C "$SRC" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='data' \
    --exclude='*.db' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    --exclude='recordings' \
    --exclude='tls' \
    . | tar -xf - -C "$OUT"

# --match: the nearest tag of ANY kind is often emos-v*, which names the wrong
# component and looks plausible (the build.sh trap in the root CLAUDE.md).
VERSION="dev-$(git -C "$SRC" describe --tags --match 'controller-v*' --match 'controller-ea-v*' --always --dirty 2>/dev/null || echo local)"

# Rewrite the add-on identity:
#   image:   REMOVED, so Supervisor builds this folder instead of pulling a
#            published tag. This is the one intentional divergence, and it is
#            the whole point of a local add-on.
#   slug:    distinct, so this installs alongside the released add-on rather
#            than replacing it.
#   version: the git description, so the add-on page says what it is.
python3 - "$OUT/config.yaml" "$VERSION" "$KEEP_SLUG" <<'PY'
import re, sys
path, version, keep_slug = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
text = open(path, encoding="utf-8").read()

# Drop `image:` and the comment block explaining it — that comment is about
# pulling a published image, which this build deliberately does not do.
text = re.sub(r'(?:^#.*\n)*^image:.*\n', '', text, count=1, flags=re.M)

text = re.sub(r'^version:.*$', f'version: "{version}"', text, count=1, flags=re.M)
if keep_slug:
    # Slug stays "controller", so a local install is local_controller rather
    # than local_controller-dev, and satellite ports stay on the GA base.
    # It does not share the git add-on's data directory. The env var makes
    # em_start refuse an empty /data instead of minting a new CA.
    if not re.search(r'^slug:\s*"controller"\s*$', text, re.M):
        sys.exit('config.yaml slug is not "controller"; --keep-slug expects that slug')
    if "EM_REQUIRE_EXISTING_DATA" not in text:
        text = re.sub(
            r'^(  DB_PATH:.*)$',
            '\\1\n  # Set only on this local replacement. em_start exits if\n'
            '  # /data has no database and device-link CA.\n'
            '  EM_REQUIRE_EXISTING_DATA: "1"',
            text, count=1, flags=re.M)
else:
    text = re.sub(r'^name:.*$', 'name: "EchoMuse (dev)"', text, count=1, flags=re.M)
    text = re.sub(r'^slug:.*$', 'slug: "controller-dev"', text, count=1, flags=re.M)
    # ESPHome on x2xx (GA x0xx, EA x1xx): HA keys a satellite on host and port, so
    # sharing a range would point HA's stored entries at whichever device holds
    # that number under dev. BLE (17201+) follows by the existing offset. The
    # controller's own ports stay: GA, EA and dev are never run together.
    text = re.sub(r'^(  esphome_port_base:) \d+$', r'\1 16201', text, count=1, flags=re.M)

open(path, "w", encoding="utf-8").write(text)

# Check the KEY, not the substring — several comments in this file mention
# `image:` while explaining the pin, and matching those made the guard fail
# on a rewrite that had worked.
port = "16001" if keep_slug else "16201"
slug = 'slug: "controller"' if keep_slug else 'slug: "controller-dev"'
missing = [k for k in ("slug:", "version:", "ingress:", f"  esphome_port_base: {port}")
           if not re.search(rf'^{k}', text, re.M)]
if missing or re.search(r'^image:', text, re.M) or not re.search(rf'^{re.escape(slug)}$', text, re.M):
    sys.exit(f"config.yaml rewrite failed: {missing or 'image: or slug'} ")
PY

echo "Local add-on written to: $OUT"
echo "  version: $VERSION"
echo
echo "Copy it to apps/local on the Home Assistant host (older builds:"
echo "addons/local), then reload the store."
echo
if [ "$KEEP_SLUG" = 1 ]; then
    echo "Slug in config.yaml stays controller. Supervisor names a local"
    echo "app local_controller. Its data directory is apps/data/local_controller"
    echo "(older Supervisor: addons/data), which starts empty. The installed"
    echo "app from the wilbowes store is acc06409_controller, a different"
    echo "directory. This build refuses to start until that data is copied."
    echo
    echo "On the Home Assistant host, from the Advanced SSH app, stop the"
    echo "original, then:"
    echo "  tools/migrate_addon_data.sh copy"
    echo "  tools/migrate_addon_data.sh check"
    echo "The script uses /mnt/data/supervisor/apps/data when that exists,"
    echo "otherwise addons/data. --data-root overrides it. copy copies the"
    echo "whole directory and sets the original app's boot to manual, so a"
    echo "reboot does not start both. check exits non-zero without the"
    echo "database, tls/, oww_models/hey_leo_v6.onnx and firmware/. The app"
    echo "does the same check at start. Do not start it before check passes."
    echo "Do not run both at once: they share ports 8767, 8770 and 8768."
    echo "Do not uninstall either one."
    echo
    echo "Rollback sets the original boot back to auto. Then stop"
    echo "local_controller and start acc06409_controller:"
    echo "  tools/migrate_addon_data.sh rollback"
    echo
    echo "Satellite ports stay on the GA base (16001)."
else
    echo "ESPHome satellites from 16201 (BLE 17201), so HA's entries for dev"
    echo "devices never collide with GA's or EA's."
    echo
    echo "NOTE: an add-on gets its OWN /data, so this starts with an EMPTY"
    echo "database and a FRESHLY GENERATED CA. A device holding the released"
    echo "add-on's CA will dial wss, fail verification and never connect — it"
    echo "picks wss from the mDNS tls_port record, not from require_device_tls,"
    echo "so turning that off does not help. To test against a real device,"
    echo "copy the released app's whole /data (database, tls/, oww_models/,"
    echo "firmware/) into the dev app's /data before starting it."
    echo
    echo "To build the replacement (slug controller, and it will not start"
    echo "until the installed add-on's data is copied), run this script"
    echo "with --keep-slug. That still uses a new data directory."
    echo
    echo "Both use host networking and the same ports, so stop one before"
    echo "starting the other."
fi
