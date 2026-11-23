"""
home-voice-hub #171: the listening ring waits for the wake check.

Since #263 an Echo draws its listening ring at its own wake crossing, before
the second-stage checker (em_wakeverify) has answered, so a rejected wake
still lit the ring. With WAKE_RING_AFTER_VERIFY on (and a checker URL set)
the Echo is pushed `listeningAnim: {}` and the controller lights the ring
itself, only after the verdict and arbitration. Off, nothing changes.

The private-listening wake path, the private barge path and the no-HA
stand-down are driven for real here with a fake checker; the stream path is
held by source guards, since _stream_listen needs a live wake model.
"""

import asyncio
import json
import logging
import re

import aiohttp
import pytest

import _oww_stub
import _sources

_oww_stub.install()

import em_controller  # noqa: E402 — must follow the stub
import em_device_config  # noqa: E402
import em_scenes  # noqa: E402
import em_state  # noqa: E402
import em_wakering  # noqa: E402
import em_wakeverify  # noqa: E402

URL = "http://checker.test/wake"


@pytest.fixture(autouse=True, scope="module")
def _drop_the_stub():
    yield
    _oww_stub.uninstall()


# ── Fakes ────────────────────────────────────────────────────────────────────

class _WS:
    """Records every control message, in order, into a shared timeline."""

    def __init__(self, timeline, device_id):
        self.timeline = timeline
        self.device_id = device_id

    async def send(self, raw):
        self.timeline.append((self.device_id, json.loads(raw)))


def _device(timeline, device_id="kitchen", caps=("led_anim",)):
    d = em_controller.Device(device_id, "10.0.0.1", list(caps), None)
    d.control_ws = _WS(timeline, device_id)
    d.led_scene = em_scenes.resolve({})
    d.wake_word_enabled = True
    d.muted = False
    d.wake_arb_ms = 700
    return d


def _ev(session=7, preroll=b"\x00\x01" * 800, barge=False):
    """An oww_wake as em_listen.parse_wake builds it. Call inside a loop."""
    return {
        "captured_mono": None, "session": session, "score": 0.91,
        "threshold": 0.5, "age_ms": 40, "floor": None, "level": None,
        "peak": None, "barge": barge,
        "arrived": asyncio.get_event_loop().time(), "preroll": preroll,
    }


def _is_ring(msg):
    return msg.get("type") == "led_anim" and \
        msg.get("anim") == em_scenes.resolve({})["listening_anim"]


def _kinds(timeline, device_id="kitchen"):
    """A readable sequence of what one device was sent, plus verdict marks."""
    out = []
    for who, msg in timeline:
        if who == "verify":
            out.append(f"verify:{msg}")
        elif who != device_id:
            continue
        elif _is_ring(msg):
            out.append("ring")
        elif msg.get("type") == "led_anim":
            out.append(f"anim:{(msg.get('anim') or {}).get('pattern')}")
        elif msg.get("type") in ("listen_ack", "listen_close", "play_cue"):
            out.append(f"{msg['type']}:{msg.get('reason', msg.get('cue', ''))}")
    return out


@pytest.fixture
def env(monkeypatch):
    """The private wake path with every collaborator outside it faked."""
    timeline = []
    state = {"serves": True, "winner": None, "verdict": True}

    monkeypatch.setattr(em_wakeverify, "_cached", em_wakeverify.Settings(
        url=URL, timeout_s=0.3, on_error="accept"))
    monkeypatch.setattr(em_wakering, "_cached", True)

    async def post(url, payload, timeout_s):
        v = state["verdict"]
        await asyncio.sleep(0.01)
        timeline.append(("verify", v if isinstance(v, bool) else v.__name__))
        if v is asyncio.TimeoutError:
            raise asyncio.TimeoutError()
        if v is aiohttp.ClientError:
            raise aiohttp.ClientError("down")
        return 200, {"accept": v}

    monkeypatch.setattr(em_wakeverify, "post_json", post)

    async def claim(device, *a, **kw):
        return state["winner"] or device.device_id

    async def turn(device, **kw):
        # The real _run_voice_locked lights the ring at turn start; held to
        # that by test_the_turn_lights_the_ring_after_the_voice_lock below.
        timeline.append((device.device_id, {"type": "turn"}))
        await em_controller.leds_listening(device)

    async def dropped(device, label, info):
        device.last_turn_outcome = "no_ha"

    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(em_controller.esphome, "can_serve_turn",
                        lambda _id: state["serves"])
    monkeypatch.setattr(em_controller.esphome, "record_dropped_wake", dropped)
    monkeypatch.setattr(em_controller.esphome, "abort_ha_run", lambda *a, **k: None)
    monkeypatch.setattr(em_controller.esphome, "cancel_voice_turn", lambda *a, **k: None)
    monkeypatch.setattr(em_controller, "_claim_wake", claim)
    monkeypatch.setattr(em_controller, "_run_voice_locked", turn)
    monkeypatch.setattr(em_controller.em_dbwriter, "submit", lambda *a, **k: None)
    monkeypatch.setattr(em_controller.em_noreply, "play", noop)
    monkeypatch.setattr(em_controller, "NO_HA_HOLD_S", 0.0)
    monkeypatch.setattr(em_state, "devices", {})
    return timeline, state


def _wake(timeline, *devices, ev=None):
    async def go():
        # Two or more Echoes, or arbitration does not run at all.
        em_state.devices["bystander"] = _device([], "bystander")
        for d in devices:
            em_state.devices[d.device_id] = d
        await asyncio.gather(*(em_controller._private_wake_turn(d, _ev(**(ev or {})))
                               for d in devices))
    asyncio.run(go())


# ── 1, 2: the push ────────────────────────────────────────────────────────────

def test_parse_needs_the_option_and_a_url():
    assert em_wakering.parse({}) is False
    assert em_wakering.parse({"WAKE_RING_AFTER_VERIFY": "1"}) is False, \
        "no checker: behave as today"
    assert em_wakering.parse({"WAKE_VERIFY_URL": URL}) is False, "default is off"
    assert em_wakering.parse({"WAKE_RING_AFTER_VERIFY": "0",
                              "WAKE_VERIFY_URL": URL}) is False
    assert em_wakering.parse({"WAKE_RING_AFTER_VERIFY": "1",
                              "WAKE_VERIFY_URL": URL}) is True
    assert em_wakering.parse({"WAKE_RING_AFTER_VERIFY": "true",
                              "WAKE_VERIFY_URL": URL}) is True


def test_option_off_pushes_the_real_spec(monkeypatch):
    monkeypatch.setattr(em_wakering, "_cached", False)
    tl = []
    d = _device(tl)
    asyncio.run(em_wakering.push_listening_anim(d))
    assert tl == [("kitchen", {"type": "config",
                               "listeningAnim": d.led_scene["listening_anim"]})]
    assert tl[0][1]["listeningAnim"].get("pattern")


def test_option_on_pushes_an_empty_spec_never_omits_the_key(monkeypatch):
    monkeypatch.setattr(em_wakering, "_cached", True)
    tl = []
    d = _device(tl)
    asyncio.run(em_wakering.push_listening_anim(d))
    assert tl == [("kitchen", {"type": "config", "listeningAnim": {}})]
    # On the wire the key is present and empty: an omitted key would leave
    # the device's old spec cached (config.go applies non-nil fields only).
    assert '"listeningAnim": {}' in json.dumps(em_wakering.listening_anim_message(d))


def test_option_on_pushes_it_even_when_the_scene_has_no_listening_anim(monkeypatch):
    tl = []
    d = _device(tl)
    d.led_scene = {k: v for k, v in d.led_scene.items() if k != "listening_anim"}
    monkeypatch.setattr(em_wakering, "_cached", False)
    assert em_wakering.listening_anim_message(d) is None, "today: nothing to send"
    monkeypatch.setattr(em_wakering, "_cached", True)
    assert em_wakering.listening_anim_message(d) == {"type": "config",
                                                      "listeningAnim": {}}


def test_firmware_without_led_anim_is_never_sent_either(monkeypatch):
    tl = []
    d = _device(tl, caps=())
    for on in (False, True):
        monkeypatch.setattr(em_wakering, "_cached", on)
        asyncio.run(em_wakering.push_listening_anim(d))
        asyncio.run(em_wakering.barge_ring(d, em_controller.leds_listening))
    assert tl == []


def test_a_live_scene_change_pushes_the_empty_spec(monkeypatch):
    monkeypatch.setattr(em_wakering, "_cached", True)

    async def noop(*a, **kw):
        return None

    monkeypatch.setattr(em_device_config.em_esphome, "update_oww_model", noop)
    tl = []
    d = _device(tl)
    asyncio.run(em_device_config.apply_live_config(
        "kitchen", d, {"ledScene": "pride"}))
    pushes = [m for _, m in tl if "listeningAnim" in m]
    assert pushes == [{"type": "config", "listeningAnim": {}}], \
        "a scene change must not hand back a real spec while the option is on"


def test_both_push_sites_go_through_the_helper():
    reg = (_sources.CONTROLLER / "em_controller.py").read_text()
    reg = reg[reg.index("device.led_scene     = em_scenes.resolve(config)"):][:600]
    assert "em_wakering.push_listening_anim(device)" in reg
    live = _sources.def_source("apply_live_config")
    assert "em_wakering.push_listening_anim(live)" in live


def test_nothing_else_sends_a_listening_spec():
    """A reconnect, a rename (sendspinName alone) or any other push must not
    carry a real spec past the option: em_wakering is the one writer."""
    # code_only unparses, which writes string literals in single quotes.
    hits = [p.name for p in _sources.MODULES
            if "'listeningAnim'" in _sources.code_only(_sources.source(p))]
    assert hits == ["em_wakering.py"]


# ── 3, 4, 5: the private wake path ───────────────────────────────────────────

def test_reject_sends_no_ring_no_ack_and_no_cue(env):
    timeline, state = env
    state["verdict"] = False
    d = _device(timeline)
    _wake(timeline, d)
    seq = _kinds(timeline)
    assert "ring" not in seq
    assert not any(s.startswith(("listen_ack", "play_cue")) for s in seq)
    assert "listen_close:rejected" in seq
    assert seq.index("verify:False") < seq.index("listen_close:rejected")


@pytest.mark.parametrize("verdict", [True, asyncio.TimeoutError, aiohttp.ClientError])
def test_accept_timeout_and_error_light_the_ring_after_the_verdict(env, verdict):
    timeline, state = env
    state["verdict"] = verdict
    d = _device(timeline)
    _wake(timeline, d)
    seq = _kinds(timeline)
    v = next(s for s in seq if s.startswith("verify:"))
    assert seq.index(v) < seq.index("listen_ack:") < seq.index("ring"), seq
    assert seq.count("ring") == 1


def test_an_arbitration_loser_gets_no_ring(env):
    timeline, state = env
    state["winner"] = "garage"
    d = _device(timeline)
    _wake(timeline, d)
    seq = _kinds(timeline)
    assert "ring" not in seq
    assert "listen_close:ceded" in seq


def test_two_dots_only_the_winner_lights(env):
    timeline, state = env
    state["winner"] = "kitchen"
    a = _device(timeline, "kitchen")
    b = _device(timeline, "garage")
    _wake(timeline, a, b)
    assert _kinds(timeline, "kitchen").count("ring") == 1
    assert "ring" not in _kinds(timeline, "garage")
    assert "listen_close:ceded" in _kinds(timeline, "garage")


def test_a_wake_with_no_clip_still_lights_at_turn_start(env):
    """Older firmware sends no pre-roll, so nothing is checked; the ring then
    comes from the turn, which is the only ring that device gets now."""
    timeline, _ = env
    d = _device(timeline)
    _wake(timeline, d, ev={"preroll": None})
    seq = _kinds(timeline)
    assert not any(s.startswith("verify:") for s in seq)
    assert seq.index("listen_ack:") < seq.index("ring")


def test_option_off_the_controller_does_not_change_the_accept_path(env, monkeypatch):
    timeline, _ = env
    monkeypatch.setattr(em_wakering, "_cached", False)
    d = _device(timeline)
    _wake(timeline, d)
    seq = _kinds(timeline)
    assert seq.index("verify:True") < seq.index("listen_ack:") < seq.index("ring")


def test_the_turn_lights_the_ring_after_the_voice_lock():
    """The fake turn above stands in for this: leds_listening is the turn's
    first LED act, inside voice_lock, after the wake path has finished."""
    body = _sources.def_source("em_controller._run_voice_locked")
    lock = body.index("async with device.voice_lock:")
    assert lock < body.index("await leds_listening(device)")


def test_private_path_waits_for_the_verdict_before_the_ack():
    body = _sources.code_only(_sources.def_source("em_controller._private_wake_turn"))
    assert body.index("em_wakeverify.finish(verify)") < body.index("listen_ack(session)") \
        < body.index("_run_voice_locked(")
    assert "leds_listening" not in body and "send_led_anim" not in body


def test_stream_path_waits_for_the_verdict_before_cue_and_turn():
    body = _sources.code_only(_sources.def_source("em_controller._stream_listen"))
    fin = body.index("em_wakeverify.finish(verify)")
    assert fin < body.index("play_cue('wake')") < body.index("_run_voice_locked(", fin)
    assert "leds_listening" not in body
    drop = _sources.code_only(_sources.def_source("em_controller._drop_rejected_wake"))
    assert "leds_listening" not in drop and "play_cue" not in drop


# ── 6b: barge-in, no-HA ──────────────────────────────────────────────────────

def _barge(timeline, d):
    async def go():
        em_state.devices[d.device_id] = d
        d.speaking = True
        d.barge_in_enabled = True
        d.barge_detected = False
        await em_controller._private_barge(d, _ev(barge=True))
    asyncio.run(go())


def test_a_private_barge_is_lit_at_once_when_the_dot_will_not_draw(env):
    timeline, _ = env
    d = _device(timeline)
    _barge(timeline, d)
    sent = [m for who, m in timeline if who == "kitchen"]
    ring = next(i for i, m in enumerate(sent) if _is_ring(m))
    flush = next(i for i, m in enumerate(sent) if m.get("type") == "speaker_flush")
    assert ring < flush, "the ring is the 'heard you' cue, before the reply stops"
    assert not any(who == "verify" for who, _ in timeline), "a barge has no check"


def test_a_private_barge_with_the_option_off_is_left_to_the_dot(env, monkeypatch):
    timeline, _ = env
    monkeypatch.setattr(em_wakering, "_cached", False)
    d = _device(timeline)
    _barge(timeline, d)
    assert "ring" not in _kinds(timeline)


def test_a_declined_barge_shows_nothing(env):
    timeline, _ = env
    d = _device(timeline)

    async def go():
        d.speaking = True
        d.barge_in_enabled = False
        await em_controller._private_barge(d, _ev(barge=True))
    asyncio.run(go())
    assert "ring" not in _kinds(timeline)


def test_the_stream_barge_path_is_untouched():
    """A streaming Echo never draws at its crossing (onWakeCrossing returns
    before StartAnim unless the wake word is on the device), and the stream
    barge watcher repaints through the interrupting turn as it always has."""
    assert "em_wakering" not in _sources.def_source("em_controller._barge_watcher")


def test_no_ha_accept_lights_the_ring_then_the_cue(env):
    timeline, state = env
    state["serves"] = False
    d = _device(timeline)
    _wake(timeline, d)
    seq = _kinds(timeline)
    assert seq.index("verify:True") < seq.index("ring") < seq.index(
        f"anim:{d.led_scene['no_ha_anim']['pattern']}"), seq
    assert "listen_close:no_ha" in seq


def test_no_ha_reject_shows_no_bare_orange_flash(env):
    timeline, state = env
    state["serves"] = False
    state["verdict"] = False
    d = _device(timeline)
    _wake(timeline, d)
    seq = _kinds(timeline)
    assert "ring" not in seq
    assert f"anim:{d.led_scene['no_ha_anim']['pattern']}" not in seq
    assert "anim:off" in seq


def test_no_ha_with_the_option_off_is_unchanged(env, monkeypatch):
    timeline, state = env
    monkeypatch.setattr(em_wakering, "_cached", False)
    state["serves"] = False
    d = _device(timeline)
    _wake(timeline, d)
    seq = _kinds(timeline)
    assert "ring" not in seq, "the Dot lit it at the crossing; the hold keeps it"
    assert f"anim:{d.led_scene['no_ha_anim']['pattern']}" in seq


def test_current_ignores_an_abandoned_or_earlier_check():
    async def go():
        async def post(url, payload, t):
            return 200, {"accept": True}
        cfg = em_wakeverify.Settings(url=URL)
        t = em_wakeverify.begin(device_id="den", score=0.9, rms=0.0, pcm=b"\0\0",
                                streaming=True, cfg=cfg, post=post)
        assert em_wakeverify.current("den") is t
        await t
        assert em_wakeverify.current("den") is t, "done is still this wake's"
        em_wakeverify.cancel(t)
        assert em_wakeverify.current("den") is None
        t2 = em_wakeverify.begin(device_id="den", score=0.9, rms=0.0, pcm=b"\0\0",
                                 streaming=True, cfg=cfg, post=post)
        await t2
        # A later wake that was not sent (no URL) must not see t2.
        em_wakeverify.begin(device_id="den", score=0.9, rms=0.0, pcm=b"",
                            streaming=True, cfg=em_wakeverify.Settings())
        assert em_wakeverify.current("den") is None
    asyncio.run(go())


# ── 6: the timing log ────────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict,line", [
    (True, r"wake verify kitchen — accept \(\d+ ms\)"),
    (False, r"wake verify kitchen — reject \(\d+ ms\)"),
    (asyncio.TimeoutError, r"wake verify kitchen timeout — accept \(\d+ ms\)"),
    (aiohttp.ClientError, r"wake verify kitchen error — accept \(\d+ ms\)"),
])
def test_the_verdict_line_carries_milliseconds(env, caplog, verdict, line):
    timeline, state = env
    state["verdict"] = verdict
    caplog.set_level(logging.INFO, logger="echomuse.wakeverify")
    _wake(timeline, _device(timeline))
    msgs = [r.getMessage() for r in caplog.records if r.name == "echomuse.wakeverify"]
    assert any(re.fullmatch(line, m) for m in msgs), msgs


def test_the_timing_log_is_on_with_the_option_off(env, caplog, monkeypatch):
    timeline, _ = env
    monkeypatch.setattr(em_wakering, "_cached", False)
    caplog.set_level(logging.INFO, logger="echomuse.wakeverify")
    _wake(timeline, _device(timeline))
    assert any(re.fullmatch(r"wake verify kitchen — accept \(\d+ ms\)", r.getMessage())
               for r in caplog.records)


# ── 7: what the firmware does with {} (no firmware change) ───────────────────

def test_the_firmware_draws_only_a_spec_with_a_pattern():
    """The whole option rests on this guard in onWakeCrossing; the Go side
    (internal/config/listening_anim_empty_test.go) proves {} replaces the
    cached spec and decodes to an empty pattern."""
    src = (_sources.CONTROLLER.parent / "device" / "cmd" / "server.go").read_text()
    fn = src[src.index("func onWakeCrossing"):]
    fn = fn[:fn.index("\nfunc ", 1)]
    draw = fn.index("srv.StartAnim(spec)")
    assert 'spec.Pattern != ""' in fn[:draw]
    cfg = (_sources.CONTROLLER.parent / "device" / "internal" / "config" / "config.go").read_text()
    assert "if msg.ListeningAnim != nil {" in cfg, \
        "{} must replace the cached spec: Apply has to test nil, not length"
