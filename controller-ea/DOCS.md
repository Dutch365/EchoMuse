# EchoMuse — Early Access

This is the **Early Access** channel: the next controller release,
before it is general. It is the same program as the stable add-on
and the same settings; only the version differs.

**Install this instead of the stable add-on, not alongside it.**
Both use host networking and the same ports (8767, 8768, 8770), so
whichever starts second will fail to bind.

**Switching channels is a migration, not a toggle.** Home Assistant
add-ons do not share storage, so this starts with an empty database
and a newly generated certificate authority. Existing devices hold
the stable add-on's CA, and they take `wss` from the mDNS record
rather than from `require_device_tls` — so they will fail
verification and will not connect at all until you copy the stable
add-on's `/data` across, including all four files in `tls/`.

**Satellite ports differ by channel**, and that is deliberate. The
stable add-on hands out 16001 upward for voice satellites and 17001
upward for Bluetooth proxies; this one uses 16101 and 17101. Home
Assistant keys an ESPHome device on its host and port, so shared
ranges would let an entry left over from the other channel connect
to a different device entirely — the wake word fires, the ring
lights, and the turn dies with no pipeline behind it. With the
ranges apart, a stale entry simply shows as unavailable.

Report anything you find against the EchoMuse repository, saying
which channel you are on.

---

# EchoMuse

Runs the EchoMuse controller — wake word detection, fleet dashboard, and
Home Assistant integration for rooted Echo Dot 2nd Gen devices — as a Home
Assistant add-on instead of a separate docker-compose deployment.

## Installation

1. Install the add-on and start it. **Controller LAN IP address** can be
   left empty — the address devices are told to dial is detected from this
   host, and the one in use is logged at startup. Set it explicitly only if
   this machine has more than one network interface.
2. Open the dashboard from **Open Web UI**. You are signed in as your Home
   Assistant user; there is no separate account or setup token. The first
   person to open it becomes the EchoMuse admin, and anyone after that gets
   read-only access until an admin promotes them.
3. Use the dashboard's **provisioning wizard** (USB, Chrome) to set up a
   rooted Echo Dot. It finds this controller automatically — no manual IP
   entry on the device side.
4. Approve the device in the dashboard once it appears as pending. Home
   Assistant then discovers it automatically via the built-in ESPHome
   integration.

### The provisioning wizard needs a secure browser context

WebUSB — which the wizard uses to talk to the Dot over the cable — is only
available on a secure origin. If you reach Home Assistant over plain
`http://`, the wizard's first step will say so and name the exact origin to
allow. Either serve Home Assistant over HTTPS, or add that origin to
`chrome://flags/#unsafely-treat-insecure-origin-as-secure` and relaunch the
browser. The allowlist matches scheme, host **and port** exactly, so an
entry for some other address does not cover it.

### Privacy

Saved utterance recordings and voice transcripts are **admin-only**. Every
Home Assistant user in the household can reach this panel, so read-only
accounts see turn timings, scores and outcomes but not the audio or the
text of what was said. Recording is off by default and stays that way until
you turn it on per device.

## Replacing an existing EchoMuse add-on

The add-on installed from the EchoMuse store is `acc06409_controller`.
Supervisor builds that name as `<repository>_<slug>`. `slug` in
`config.yaml` is `controller`. For a git store the repository id is the
first 8 hex characters of SHA-1 of the repository URL, lowercased
(`get_hash_from_repository` in `supervisor/store/utils.py`).
`https://github.com/wilbowes/EchoMuse` hashes to `acc06409`.

A folder copied to the local apps directory is repository `local`, so the
same `slug: controller` becomes `local_controller`. Supervisor stores each
app's files at `apps/data/<that full name>` (`path_data`). On this
Supervisor that is `/mnt/data/supervisor/apps/data`, and the local app
source is `/mnt/data/supervisor/apps/local`. Older builds use `addons/data`
and `addons/local`. `migrate_addon_data.sh` uses `apps/` when that
directory exists, and `addons/` otherwise. `--data-root` overrides it.

`apps/data/local_controller` is a new directory. It is not
`apps/data/acc06409_controller`. Starting the local app while it is empty
creates a new database and a new CA. With device approval set to strict,
every Dot then comes up unpaired. Device config also points at
`/data/oww_models/hey_leo_v6.onnx`, and firmware is read from `/data/firmware/`,
so those have to be in the copy too.

Stop the original app. Do not uninstall it. Uninstall deletes its data
directory. From the Advanced SSH app (it has `SUPERVISOR_TOKEN` and can
see `/mnt/data/supervisor`):

```bash
controller/tools/make_dev_addon.sh --keep-slug
controller/tools/migrate_addon_data.sh copy
controller/tools/migrate_addon_data.sh check
```

Copy the folder `make_dev_addon.sh` prints into `apps/local` and reload
the store, then install it. `copy` sets the original app's boot to
`manual` (`POST /apps/acc06409_controller/options` with `{"boot":"manual"}`)
and then copies the whole data directory. A reboot does not start the
original on ports 8767, 8770 and 8768 beside the local app. `check` exits non-zero unless the target has `echomuse.db`,
`tls/`, `oww_models/hey_leo_v6.onnx` and a non-empty `firmware/`. The app
itself exits the same way (`EM_REQUIRE_EXISTING_DATA`) if it is started
anyway.

Start `local_controller` only after `check` passes, and only while the
original is stopped.

The copy leaves the original directory where it is. Rollback stops the
local app, sets the original boot back to `auto`, and starts the original:

```bash
controller/tools/migrate_addon_data.sh rollback
```

That command checks the original directory is still intact and does not
move it. This patch does not change the database schema and does not
change the device firmware.

The `image:` pin in `config.yaml` names the published upstream image. A
local add-on drops that pin and builds this tree.

## Publishing the fork does not reuse that data directory

Putting this fork up as its own Home Assistant add-on repository does not
land on `acc06409_controller`. The prefix is a hash of the repository URL
Supervisor was given, not of the `slug` field, and `config.yaml` has no
field that sets it. `https://github.com/Dutch365/EchoMuse` hashes to
`84c6fece`, so that store's add-on is `84c6fece_controller` with a new
empty `apps/data/84c6fece_controller`. The same URL with a `.git` suffix
is a third id (`93341de2`). Letter case does not change the hash;
Supervisor lowercases the URL first. The only URL that maps to `acc06409`
is the existing wilbowes store URL. The database and `tls/` still have to
be copied, the same as for the local add-on.

## Second-stage wake check

Leave **Second-stage wake check URL** empty and nothing is posted. Set it
to your checker. A streaming wake (the controller is scoring, or shadow
mode) is sent as JSON: device id, score, frame RMS, and about 1.5 seconds
of audio in `audio_wav_b64`. Reply with `{"accept": true}` or
`{"accept": false}` within the timeout (default 300 ms). If the checker
does not answer, the wake is accepted unless you set the error policy to
reject.

An Echo that listens privately is not sent. It does not stream audio
before its own wake word, so there is no clip on this side.

## Configuration

Every option is explained inline in the add-on's Configuration tab. For the
full picture — rooting a device, the voice pipeline, every configuration
knob — see the project's own docs:

- [Quickstart](https://github.com/wilbowes/EchoMuse/blob/main/docs/quickstart.md)
- [Configuration reference](https://github.com/wilbowes/EchoMuse/blob/main/docs/configuration.md)
- [Rooting a device](https://github.com/wilbowes/EchoMuse/blob/main/docs/rooting.md)

**Note**: restart the add-on after changing configuration.
