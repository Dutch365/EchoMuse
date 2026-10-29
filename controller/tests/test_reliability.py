"""Voice-reliability switches stay shadow, log, or off unless asked.

Replay figures are the 2026-10-16 study's published numbers, in
fixtures/reliability/replay.json. The overnight logs themselves were not
in the tree.
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest
import yaml

import em_arbiter
import em_reliability
import em_speechgate
import em_wakeverify
from em_arbiter import Claim, WakeArbiter, pick

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "reliability" / "replay.json").read_text())
CONTROLLER = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _quiet_house():
    em_reliability.reset()
    yield
    em_reliability.reset()
    for key in ("living", "kitchen", "dot-a", "bedroom"):
        em_wakeverify._accept_gen.pop(key, None)
        em_wakeverify._inflight.pop(key, None)


def _claims(rows):
    return [Claim(r["heard"], r["device"], r["level"], score=r["score"],
                  floor_db=r["floor_db"]) for r in rows]


def test_every_switch_defaults_to_shadow_log_or_off(monkeypatch):
    for name in ("EM_ARB_SCORE_MODE", "EM_LOSER_COOLDOWN_MODE",
                 "EM_LOSER_COOLDOWN_S", "EM_LOSER_OVERRIDE_WAKE",
                 "EM_EXTERNAL_AUDIO_GATE", "EM_ROOM_MEDIA_PLAYERS",
                 "EM_EXTERNAL_AUDIO_FAINT_DBFS", "EM_ADAPTIVE_ENDPOINT",
                 "EM_ADAPTIVE_NO_SPEECH_MS", "EM_ADAPTIVE_VAD_STEP"):
        monkeypatch.delenv(name, raising=False)
    assert em_reliability.arb_mode() == "shadow"
    assert em_reliability.cool_mode() == "log"
    assert em_reliability.ext_mode() == "log"
    assert em_reliability.adaptive_enabled() is False
    assert em_reliability.cooldown_s() == 7
    assert em_reliability.override_wake() == 0.95
    assert em_reliability.faint_dbfs() == -40
    assert em_reliability.adaptive_no_speech_s() == 3
    assert em_reliability.adaptive_vad_step() == 1
    assert em_reliability.room_map() == {}


def test_garbage_env_keeps_the_defaults():
    env = {
        "EM_ARB_SCORE_MODE": "nope",
        "EM_LOSER_COOLDOWN_MODE": "",
        "EM_EXTERNAL_AUDIO_GATE": "LIVE",
        "EM_LOSER_COOLDOWN_S": "99",
        "EM_LOSER_OVERRIDE_WAKE": "nope",
        "EM_ADAPTIVE_ENDPOINT": "0",
        "EM_ROOM_MEDIA_PLAYERS": "{",
    }
    assert em_reliability.arb_mode(env) == "shadow"
    assert em_reliability.cool_mode(env) == "log"
    assert em_reliability.ext_mode(env) == "live"
    assert em_reliability.cooldown_s(env) == 10
    assert em_reliability.override_wake(env) == 0.95
    assert em_reliability.adaptive_enabled(env) is False
    assert em_reliability.room_map(env) == {}
    assert em_reliability.cooldown_s({"EM_LOSER_COOLDOWN_S": "1"}) == 5


def test_addon_options_use_those_defaults():
    cfg = yaml.safe_load((CONTROLLER / "config.yaml").read_text())
    opt = cfg["options"]
    assert cfg["version"] == "2.26.8"
    assert opt["arb_score_mode"] == "shadow"
    assert opt["loser_cooldown_mode"] == "log"
    assert opt["loser_cooldown_s"] == 7
    assert opt["loser_override_wake"] == 0.95
    assert opt["external_audio_gate"] == "log"
    assert opt["room_media_players"] == ""
    assert opt["external_audio_faint_dbfs"] == -40
    assert opt["adaptive_endpoint"] is False
    assert opt["adaptive_no_speech_ms"] == 3000
    assert opt["adaptive_vad_step"] == 1
    assert "## 2.26.8" in (CONTROLLER / "CHANGELOG.md").read_text()


def test_replay_1039_kitchen_wins_and_the_loser_is_logged_four_seconds_later():
    case = FIXTURE["1039"]
    claims = _claims(case["claims"])
    winner, _why = pick(claims)
    assert winner.device_id == case["loudness_winner"] == "kitchen"
    report = em_reliability.evaluate(claims, winner.device_id)
    assert report["shadow"] == "kitchen"
    assert report["agree"] is True
    line = em_reliability.shadow_line(report)
    assert "arb_shadow_winner=kitchen" in line and "agree" in line
    em_reliability.record_losers(claims, "kitchen", now=0.0, window_s=7)
    later = case["later"]
    assert em_reliability.cooldown_action(
        later["device"], later["score"], later["at_s"], later["accept"]) == (
            "would_suppress")
    assert em_reliability.cooldown_action("living", 0.4, 8.0, False) == "allow"
    assert em_reliability.cooldown_action("living", 0.95, 4.0, True) == "override"
    assert em_reliability.cooldown_action("living", 0.95, 4.0, False) == (
        "would_suppress")


def test_replay_0930_agrees_with_loudness():
    case = FIXTURE["0930"]
    claims = _claims(case["claims"])
    winner, _why = pick(claims)
    assert winner.device_id == "kitchen"
    assert em_reliability.evaluate(claims, winner.device_id)["agree"] is True


def test_rooms_eight_seconds_apart_both_proceed():
    em_reliability.record_losers(
        [Claim(0.0, "living", -28.0)], "kitchen", now=0.0, window_s=7)
    assert em_reliability.cooldown_action("dining", 0.5, 1.0, False) == "allow"
    assert em_reliability.cooldown_action("living", 0.5, 8.0, False) == "allow"


def test_off_mode_does_not_suppress():
    em_reliability.record_losers(
        [Claim(0.0, "living", -28.0)], "kitchen", now=0.0, window_s=7)
    assert em_reliability.cooldown_action(
        "living", 0.2, 1.0, False, mode="off") == "allow"


def test_live_cooldown_drops_unless_the_verifier_accepts():
    em_reliability.record_losers(
        [Claim(0.0, "living", -28.0)], "kitchen", now=0.0, window_s=7)
    assert em_reliability.cooldown_action(
        "living", 0.4, 4.0, False, mode="live") == "suppress"
    assert em_reliability.cooldown_action(
        "living", 0.96, 4.0, True, mode="live") == "override"


def test_log_mode_would_suppress_and_live_mode_drops(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="echomuse")
    em_reliability.record_losers(
        [Claim(0.0, "living", -28.0)], "kitchen", now=0.0, window_s=7)

    async def log_mode():
        return await em_reliability.should_drop("living", 0.4, now=4.0, accept=False)

    assert asyncio.run(log_mode()) is False
    assert "would_suppress" in caplog.text

    monkeypatch.setenv("EM_LOSER_COOLDOWN_MODE", "live")

    async def live_mode():
        return await em_reliability.should_drop("living", 0.4, now=4.0, accept=False)

    assert asyncio.run(live_mode()) is True


def test_live_high_score_waits_for_the_checker(monkeypatch):
    monkeypatch.setenv("EM_LOSER_COOLDOWN_MODE", "live")
    em_reliability.record_losers(
        [Claim(0.0, "living", -28.0)], "kitchen", now=0.0, window_s=7)

    async def main():
        async def accept():
            await asyncio.sleep(0.02)
            em_wakeverify._gen["living"] = 1
            em_wakeverify._note_explicit("living", 1)
        em_wakeverify._inflight["living"] = asyncio.create_task(accept())
        return await em_reliability.should_drop("living", 0.96, now=4.0)

    assert asyncio.run(main()) is False
    assert em_wakeverify.accepted("living") is True


def test_tv_noise_at_minus_12_and_minus_28_and_a_faint_minus_50():
    levels = FIXTURE["synthetic_dbfs"]
    assert levels == [-12, -28, -50]
    loud = Claim(0.0, "tv", level=-12.0, score=0.4, floor_db=None)
    clean = Claim(0.023, "kitchen", level=-28.0, score=0.9, floor_db=-50.0)
    winner, _why = pick([loud, clean])
    assert winner.device_id == "tv"
    report = em_reliability.evaluate([loud, clean], winner.device_id)
    assert report["shadow"] == "kitchen"
    assert report["agree"] is False
    assert "disagree" in em_reliability.shadow_line(report)

    tv = Claim(0.0, "tv", level=-12.0, score=0.5, floor_db=-13.0)
    kitchen = Claim(0.021, "kitchen", level=-28.0, score=0.9, floor_db=-50.0)
    winner, _why = pick([tv, kitchen])
    assert winner.device_id == "kitchen"
    assert em_reliability.evaluate([tv, kitchen], "kitchen")["agree"] is True

    faint = FIXTURE["0949"]
    assert faint["level"] == -50.0
    assert em_reliability.external_verdict(
        faint["level"], faint["media_state"],
        speech_failed=not faint["speech_gate_open"]) == "would_drop"


def test_external_gate_needs_playing_and_faint_and_a_closed_speech_gate():
    assert em_reliability.external_verdict(-50, "playing", True) == "would_drop"
    assert em_reliability.external_verdict(-40, "playing", True) == "would_drop"
    assert em_reliability.external_verdict(-12, "playing", True) == "allow"
    assert em_reliability.external_verdict(-28, "playing", True) == "allow"
    assert em_reliability.external_verdict(-50, "idle", True) == "allow"
    assert em_reliability.external_verdict(-50, None, True) == "allow"
    assert em_reliability.external_verdict(-50, "playing", False) == "allow"
    assert em_reliability.external_verdict(
        -50, "playing", True, mode="off") == "allow"
    assert em_reliability.external_verdict(
        -50, "playing", True, mode="live") == "drop"


def test_external_clause_is_empty_until_the_three_conditions_hold():
    class Dev:
        device_id = "bedroom"
    assert em_reliability.external_clause(Dev()) == ""
    em_reliability.remember_media("bedroom", -50, "playing", mode="log")
    assert em_reliability.external_clause(Dev()) == " external_audio would_drop"
    em_reliability.remember_media("bedroom", -50, "playing", mode="live")
    assert em_reliability.external_clause(Dev()) == " external_audio drop"
    em_reliability.remember_media("bedroom", -12, "playing", mode="log")
    assert em_reliability.external_clause(Dev()) == ""


def test_media_read_timeout_and_errors_fail_open():
    async def slow(_entity):
        await asyncio.sleep(1)
        return "playing"

    async def boom(_entity):
        raise OSError("down")

    async def ok(_entity):
        return "playing"

    async def main():
        timed = await em_reliability.read_media_state(
            "media_player.tv", timeout_s=0.05, client=slow)
        erred = await em_reliability.read_media_state(
            "media_player.tv", client=boom)
        played = await em_reliability.read_media_state(
            "media_player.tv", client=ok)
        missing = await em_reliability.read_media_state(
            "media_player.tv", env={})
        return timed, erred, played, missing

    assert asyncio.run(main()) == (None, None, "playing", None)


def test_supervisor_read_parses_state_and_fails_open(monkeypatch):
    class Resp:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        async def json(self, content_type=None):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Sess:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, headers=None):
            assert headers["Authorization"].startswith("Bearer ")
            if url.endswith("media_player.tv"):
                return Resp(200, {"state": "playing"})
            if url.endswith("media_player.bad"):
                return Resp(500, {})
            return Resp(200, ["nope"])

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", Sess)
    monkeypatch.setattr(aiohttp, "ClientTimeout", lambda **kw: None)

    async def main():
        playing = await em_reliability.read_media_state(
            "media_player.tv", env={"SUPERVISOR_TOKEN": "tok"})
        http = await em_reliability.read_media_state(
            "media_player.bad", env={"SUPERVISOR_TOKEN": "tok"})
        odd = await em_reliability.read_media_state(
            "media_player.odd", env={"SUPERVISOR_TOKEN": "tok"})
        return playing, http, odd

    assert asyncio.run(main()) == ("playing", None, None)


def test_replay_0949_bedroom_false_wake_would_drop():
    case = FIXTURE["0949"]
    assert case["verifier"] == 0.11
    assert em_reliability.external_verdict(
        case["level"], case["media_state"], True, mode="log") == "would_drop"


def test_replay_1108_stays_put_and_1137_extends_when_asked():
    early = FIXTURE["1108"]
    later = FIXTURE["1137"]
    assert em_reliability.adapted_no_speech(
        early["adaptive"], early["explicit_accept"], early["early_snr_db"],
        early["base_no_speech_s"], 3.0, early["max_listen_s"]) == 1.5
    assert em_reliability.adapted_no_speech(
        later["adaptive"], later["explicit_accept"], later["early_snr_db"],
        later["base_no_speech_s"], later["adaptive_no_speech_s"],
        later["max_listen_s"]) == 3.0
    assert em_reliability.adapted_no_speech(
        True, True, 3.0, 1.5, 20.0, 7.0) == 7.0
    assert em_reliability.adapted_no_speech(
        True, True, 12.0, 1.5, 3.0, 15.0) == 1.5
    assert em_reliability.adapted_no_speech(
        True, False, 3.0, 1.5, 3.0, 15.0) == 1.5
    assert em_reliability.adapted_no_speech(
        False, True, 3.0, 5.0, 3.0, 15.0) == 5.0


def test_arm_turn_lowers_the_gate_only_after_an_explicit_accept(monkeypatch):
    class Dev:
        device_id = "kitchen"
        speech_gate_threshold = 0.5
        early_snr_db = 3.0
        max_listen_s = 7.0

    monkeypatch.delenv("EM_ADAPTIVE_ENDPOINT", raising=False)
    device = Dev()
    device.adaptive_gate_threshold = 0.1
    em_reliability.arm_turn(device)
    assert device.adaptive_gate_threshold is None
    assert em_speechgate.threshold_for(device) == 0.5
    assert em_reliability.no_speech_limit(device, 5.0) == 5.0

    monkeypatch.setenv("EM_ADAPTIVE_ENDPOINT", "1")
    em_reliability.arm_turn(device)
    assert device.adaptive_gate_threshold is None

    em_wakeverify._gen["kitchen"] = 1
    em_wakeverify._note_explicit("kitchen", 1)
    em_reliability.arm_turn(device)
    assert device.adaptive_gate_threshold == pytest.approx(0.45)
    assert em_speechgate.threshold_for(device) == pytest.approx(0.45)
    assert em_reliability.no_speech_limit(device, 1.5) == 3.0
    assert em_reliability.no_speech_limit(device, 5.0) == 5.0


def test_room_oww_line_and_the_wake_logs_carry_it():
    assert em_reliability.room_line("kitchen", 0.82) == "[kitchen] room_oww=0.820"
    assert em_reliability.room_line("kitchen", None) == "[kitchen] room_oww=none"
    src = (CONTROLLER / "em_controller.py").read_text()
    assert src.count("room_oww=") >= 2
    body = (CONTROLLER / "em_esphome.py").read_text()
    assert "em_reliability.arm_turn(device)" in body
    assert "em_reliability.external_clause(device)" in body
    assert "em_reliability.no_speech_limit(" in body


def test_claim_keeps_the_early_exit():
    import _sources
    body = _sources.def_source("em_controller._claim_wake")
    assert "early_s=em_listen.arbitration_early()" in body
    assert "em_reliability.should_drop" in body
    assert "em_reliability.note_claim" in body


def test_shadow_hook_logs_and_live_hook_moves_the_winner(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="echomuse")

    async def race(mode):
        monkeypatch.setenv("EM_ARB_SCORE_MODE", mode)
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        opened = loop.time()

        async def loud():
            return await arb.contest(
                "tv", 1.0, heard_at=opened, hold_s=0.15,
                level=-12.0, score=0.4)

        async def clean():
            await asyncio.sleep(0.01)
            return await arb.contest(
                "kitchen", 1.0, heard_at=opened + 0.01, hold_s=0.15,
                level=-28.0, score=0.9, floor_db=-50.0)

        return await asyncio.gather(loud(), clean())

    assert asyncio.run(race("shadow")) == ["tv", "tv"]
    assert "arb_shadow_winner=kitchen" in caplog.text
    assert "disagree" in caplog.text
    assert asyncio.run(race("live")) == ["kitchen", "kitchen"]
    assert asyncio.run(race("off")) == ["tv", "tv"]


def test_a_missing_or_raising_hook_keeps_the_loudness_winner():
    saved = em_arbiter.score_hook

    async def once(hook):
        em_arbiter.score_hook = hook
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        opened = loop.time()

        async def a():
            return await arb.contest(
                "tv", 1.0, heard_at=opened, hold_s=0.12, level=-12.0)

        async def b():
            await asyncio.sleep(0.01)
            return await arb.contest(
                "kitchen", 1.0, heard_at=opened + 0.01, hold_s=0.12, level=-28.0)

        won = await asyncio.gather(a(), b())
        return won, arb.last_entries

    try:
        won, entries = asyncio.run(once(None))
        assert won == ["tv", "tv"]
        assert [c.device_id for c in entries] == ["tv", "kitchen"]

        def boom(_entries, _winner):
            raise RuntimeError("score failed")

        won, _entries = asyncio.run(once(boom))
        assert won == ["tv", "tv"]
    finally:
        em_arbiter.score_hook = saved


def test_begin_registers_inflight_and_accepted_does_not_consume():
    cfg = em_wakeverify.parse_settings({"WAKE_VERIFY_URL": "http://checker"})

    async def post(url, payload, timeout_s):
        return 200, {"accept": True}

    async def main():
        task = em_wakeverify.begin(
            device_id="dot-a", score=0.99, rms=0.01, pcm=b"",
            streaming=True, cfg=cfg, post=post)
        assert em_wakeverify.inflight("dot-a") is task
        assert await em_wakeverify.finish(task) is True

    asyncio.run(main())
    assert em_wakeverify.accepted("dot-a") is True
    assert em_wakeverify.inflight("dot-a") is None
    assert em_wakeverify.consume_accept("dot-a") is True
    assert em_wakeverify.accepted("dot-a") is False


def test_edges_of_the_parsers_the_cooldown_and_the_media_read(monkeypatch):
    assert em_reliability.override_wake({"EM_LOSER_OVERRIDE_WAKE": "1.5"}) == 1.0
    assert em_reliability.override_wake({"EM_LOSER_OVERRIDE_WAKE": "nope"}) == 0.95
    assert em_reliability.cooldown_s({"EM_LOSER_COOLDOWN_S": "nope"}) == 7
    assert em_reliability.faint_dbfs({"EM_EXTERNAL_AUDIO_FAINT_DBFS": "-100"}) == -90
    assert em_reliability.adaptive_no_speech_s({"EM_ADAPTIVE_NO_SPEECH_MS": "10"}) == 0.5
    assert em_reliability.adaptive_vad_step({"EM_ADAPTIVE_VAD_STEP": "9"}) == 5
    assert em_reliability.adaptive_enabled({"EM_ADAPTIVE_ENDPOINT": "yes"}) is True
    assert em_reliability.room_map({"EM_ROOM_MEDIA_PLAYERS": "[1]"}) == {}
    assert em_reliability.room_map({"EM_ROOM_MEDIA_PLAYERS": '{"a": ""}'}) == {}
    assert em_reliability._now() == 0.0
    assert em_reliability.held_winner("nobody") == ""
    assert em_reliability.contest_hook(
        [Claim(0.0, "kitchen", -12.0)], "kitchen") is None

    class Dev:
        device_id = "living"

    async def inside():
        device = Dev()
        em_reliability.note_claim(device, "kitchen", -20.0, -40.0)
        assert device.early_snr_db == pytest.approx(20.0)
        assert em_reliability.held_winner("living") == "kitchen"
        em_reliability.note_claim(device, "living", None, None)
        assert device.early_snr_db is None
        dropped = await em_reliability.should_drop(
            "living", 0.2, env={"EM_LOSER_COOLDOWN_MODE": "off"})
        assert dropped is False
        assert await em_reliability.should_drop("nobody", 0.2) is False
        monkeypatch.setenv("EM_LOSER_COOLDOWN_MODE", "live")
        em_reliability.record_losers(
            [Claim(0.0, "living", -28.0)], "kitchen", now=0.0, window_s=100)

        async def bad():
            raise RuntimeError("checker down")

        em_wakeverify._inflight["living"] = asyncio.create_task(bad())
        assert await em_reliability.should_drop("living", 0.96, now=1.0) is True
        assert await em_reliability.should_drop("living", 0.2, now=1.0) is True

        em_reliability.prime_external("bedroom", None)
        em_reliability.prime_external(
            "bedroom", -10, env={"EM_EXTERNAL_AUDIO_GATE": "off"})
        em_reliability.prime_external("bedroom", -50, env={})
        em_reliability.prime_external(
            "bedroom", -50,
            env={"EM_ROOM_MEDIA_PLAYERS": '{"bedroom": "media_player.bed"}'})
        await asyncio.sleep(0.05)
        return True

    assert asyncio.run(inside()) is True
    em_reliability.prime_external(
        "bedroom", -50,
        env={"EM_ROOM_MEDIA_PLAYERS": '{"bedroom": "media_player.bed"}'})

    class High:
        device_id = "kitchen"
        speech_gate_threshold = 0.5
        early_snr_db = 12.0

    em_wakeverify._gen["kitchen"] = 1
    em_wakeverify._note_explicit("kitchen", 1)
    high = High()
    em_reliability.arm_turn(high, env={"EM_ADAPTIVE_ENDPOINT": "1"})
    assert high.adaptive_gate_threshold is None

    import aiohttp

    class Boom:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("no supervisor")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(aiohttp, "ClientSession", Boom)
    monkeypatch.setattr(aiohttp, "ClientTimeout", lambda **kw: None)

    async def failed():
        return await em_reliability.read_media_state(
            "media_player.tv", env={"SUPERVISOR_TOKEN": "tok"})

    assert asyncio.run(failed()) is None


def test_a_claim_with_no_level_does_not_invent_a_shadow_winner():
    claims = [Claim(0.0, "a", level=None, score=0.9),
              Claim(0.01, "b", level=-20.0, score=0.2, floor_db=-40.0)]
    report = em_reliability.evaluate(claims, "a")
    assert report["shadow"] == "a"
    assert report["agree"] is True
    assert em_reliability.room_map(
        {"EM_ROOM_MEDIA_PLAYERS": '{"kitchen": "media_player.kitchen"}'}) == {
            "kitchen": "media_player.kitchen"}
