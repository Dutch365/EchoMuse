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

This build keeps slug `controller`, the same slug as the upstream add-on.
Home Assistant Supervisor stores each add-on's files at `addons/data/<slug>`
(the `path_data` property in Supervisor's `addon.py`). The database
(`echomuse.db`) and the device-link CA (`tls/`) live there. The same slug
is the same directory, so Dots that are already paired stay paired.

Stop the add-on before starting the replacement. Both use host networking
and ports 8767, 8770 and 8768.

Uninstall deletes `addons/data/controller`. If you do have to uninstall,
copy that directory aside first and put it back before the first start of
the new build. A local folder that keeps the slug is:

```bash
controller/tools/make_dev_addon.sh --keep-slug
```

The `image:` pin in `config.yaml` names the published upstream image. A
local add-on drops that pin and builds this tree, which is how this patch
is installed before an image exists.

This patch does not change the database schema and does not change
firmware. Rolling back is installing controller 2.26.1 with the same slug
and the same data directory.

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
