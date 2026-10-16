# Wake preroll: one Dot, then back

This firmware keeps about 1.5s of wake audio in memory and sends it once, on `oww_wake`, when the controller has announced `wake_preroll`. Private listening is unchanged: nothing streams before the wake, and the session still starts after the crossing. The controller hands that clip to the second-stage checker (`WAKE_VERIFY_URL`). The checker stays off when the URL is empty, and a timeout or a transport error accepts.

The build to try is `server` from this change. The image to put back is the published **v2.18.1** `server` asset, saved beside it as `server-v2.18.1`. Both are ARM ELF binaries. Confirm each one before it goes near a Dot:

```bash
file server server-v2.18.1
# both: ELF 32-bit LSB shared object, ARM, EABI5, interpreter /system/bin/linker
strings server | grep -E 'v2\.18\.1-[0-9]+-g[0-9a-f]+'
strings server-v2.18.1 | grep -F 'v2.18.1'
```

The new binary's version is `git describe --tags --match 'v*'` (a `v2.18.1-N-gSHA` string). The v2.18.1 asset was built on the tag, so its version is exactly `v2.18.1` and the uploader will not extract one: a bare `vX.Y.Z` collides with dependency versions inside a Go binary. That is expected. The dashboard then labels the upload `local-<timestamp>` and, after a successful re-flash, says the Dot reconnected on `v2.18.1` and that the label was generated. That line means the re-flash worked.

## Install on one Dot

The Dot should be online, on **v2.18.1**, and the only device you touch. Use that device's own page. The fleet control (`POST /api/releases/deploy`) updates every device; do not use it.

1. Open the Dot → **Updates** → **Local Build** → **Choose file** → the new `server` → **Deploy**.
2. Leave it powered. The controller writes the inactive A/B slot, checks the md5, flips the symlink, and restarts the process. The other slot keeps v2.18.1.
3. Wait until the page says it is running the new version (`v2.18.1-N-gSHA`). The Updates row **Rollback slot** should read `v2.18.1`.

The same two calls, with an admin session token:

```bash
TOKEN=...          # Authorization: Bearer, from a dashboard login
BASE=http://controller:8767
DEV=G0A0...        # that one device id

curl -sS -H "Authorization: Bearer $TOKEN" \
  -F binary=@server "$BASE/api/releases/upload"
# {"upload_token":"...","version":"v2.18.1-N-gSHA",...}  — the token lasts 10 minutes

curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"upload_token":"<token>"}' \
  "$BASE/api/devices/$DEV/update"
```

`already_running` means this Dot already reports that version. Re-flashing it needs `"force": true` in the body. Do not pass `force` on the first try.

The controller this Dot talks to has to be this change, or a later one. Older controllers do not announce `wake_preroll`, and this firmware then sends the wake exactly as v2.18.1 did.

## Roll back

**First, flip the slot.** On the same Updates tab, **Roll back to v2.18.1**. That is a symlink flip and a restart. No binary is transferred. It is there only after the install above recorded `firmware_previous`.

```bash
curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{}' "$BASE/api/devices/$DEV/rollback"
```

The Dot should come back reporting `v2.18.1`.

**If that button is missing, or the inactive slot is not v2.18.1,** deploy `server-v2.18.1` the same way as the new binary (Choose file → Deploy). The upload's version will show as unknown / `local-<timestamp>`. After reboot the firmware row should read `v2.18.1`. The dashboard note that the expected label was `local-...` is the success case for this file, because the tag build has no `-N-gSHA` suffix for the uploader to read.

A binary that exits three times on start is flipped back by the device's own start script. That is the supervisor's rollback, separate from the button.
