import asyncio

from em_arbiter import WakeArbiter

WINDOW = 0.2


def run(coro):
    return asyncio.run(coro)


def test_solo_device_wins_immediately():
    """The winner must not wait. The old design awaited the full window on
    every wake (~364ms measured in the field, on every device) — that is
    the regression this redesign exists to remove."""
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        won = arb.claim("office", WINDOW)
        return won, loop.time() - t0
    won, elapsed = run(main())
    assert won == "office"
    assert elapsed < 0.01


def test_second_device_in_window_loses():
    async def main():
        arb = WakeArbiter()
        first = arb.claim("office", WINDOW)
        second = arb.claim("lounge", WINDOW)
        return first, second
    first, second = run(main())
    assert first == "office"
    assert second == "office"   # lounge told to stand down


def test_three_way_only_first_answers():
    """The 2026-07-20 field case: three devices within 184ms."""
    async def main():
        arb = WakeArbiter()
        return [arb.claim(d, WINDOW) for d in ("office", "lounge", "retreat")]
    assert run(main()) == ["office"] * 3


def test_loser_never_waits_either():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", WINDOW)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        arb.claim("lounge", WINDOW)
        return loop.time() - t0
    assert run(main()) < 0.01


def test_claim_expires_after_window():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 0.05)
        await asyncio.sleep(0.08)
        return arb.claim("lounge", 0.05)
    assert run(main()) == "lounge"


def test_same_device_rewaking_is_not_suppressed():
    """A device answering twice in a row is a real second utterance in the
    room it already serves — it must never be told it lost to itself."""
    async def main():
        arb = WakeArbiter()
        arb.claim("office", WINDOW)
        return arb.claim("office", WINDOW)
    assert run(main()) == "office"


def test_release_frees_the_claim_early():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 10.0)   # long window
        arb.release("office")
        return arb.claim("lounge", 10.0)
    assert run(main()) == "lounge"


def test_release_by_non_holder_is_ignored():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 10.0)
        arb.release("lounge")       # not the holder — must be a no-op
        return arb.claim("retreat", 10.0)
    assert run(main()) == "office"


def test_window_rearms_on_each_win():
    """Successive utterances in the same room keep extending suppression,
    so a distant device echoing the winner's TTS stays quiet."""
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 0.15)
        await asyncio.sleep(0.10)
        arb.claim("office", 0.15)   # re-arms from now
        await asyncio.sleep(0.10)
        return arb.claim("lounge", 0.15)
    assert run(main()) == "office"


def test_zero_window_suppresses_nothing():
    async def main():
        arb = WakeArbiter()
        arb.claim("office", 0.0)
        return arb.claim("lounge", 0.0)
    assert run(main()) == "lounge"


# ── capture time (docs/listening.md, "Arbitration") ─────────────────────────

def test_late_claim_heard_first_still_cedes_to_the_granted_one():
    """A granted claim is never revoked, even to an Echo that heard first."""
    async def main():
        arb = WakeArbiter()
        now = asyncio.get_running_loop().time()
        first = arb.claim("office", WINDOW, heard_at=now)
        second = arb.claim("lounge", WINDOW, heard_at=now - 0.05)
        return first, second
    assert run(main()) == ("office", "office")


def test_claim_delayed_in_flight_cedes_within_slack():
    """The bug this fixes: heard 50ms after the winner, arriving a second
    late, it used to fall outside the window and start a second answer."""
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t = loop.time()
        arb.claim("office", WINDOW, heard_at=t)
        await asyncio.sleep(0.4)            # past the window by arrival
        late = arb.claim("lounge", WINDOW, heard_at=t + 0.05, slack_s=1.0)
        return late
    assert run(main()) == "office"


def test_without_slack_a_late_claim_is_a_new_utterance():
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        arb.claim("office", WINDOW, heard_at=t)
        await asyncio.sleep(0.4)
        return arb.claim("lounge", WINDOW, heard_at=t + 0.05)
    assert run(main()) == "lounge"


def test_separate_utterance_heard_outside_the_window_wins():
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        arb.claim("office", WINDOW, heard_at=t - 1.0, slack_s=3.0)
        return arb.claim("lounge", WINDOW, heard_at=t, slack_s=3.0)
    assert run(main()) == "lounge"


# ── a mixed fleet: one Echo wakes itself, one is scored here ────────────────

def _mixed_pair(date_by_capture: bool) -> str:
    """Office wakes on the Echo; Lounge streams and is scored here. Both heard
    the same utterance at the same instant, but Lounge's crossing frame was
    held 1s in a retransmit and then waited behind a backlog — the two things
    measured on the bench, 2026-09-22."""
    import em_listen

    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t = loop.time()
        clock = em_listen.CaptureClock()
        # Lounge's stream up to the wake: on time, a few ms of transit.
        for n in range(40):
            clock.observe(n, t - (40 - n) * 0.08 + 0.004)
        # Office's wake arrives 150ms after capture, reporting its age.
        arb.claim("office", 0.3, heard_at=em_listen.heard_at(t, 110, 80),
                  slack_s=1.0)
        # Lounge's crossing frame (captured at t) arrives a second late...
        await asyncio.sleep(1.0)
        arrived = loop.time()
        frame = em_listen.Frame(b"\0" * 2560, arrived, clock.observe(40, arrived))
        # ...and waits another 0.5s to be scored.
        await asyncio.sleep(0.5)
        heard = em_listen.captured(frame, 0) if date_by_capture else loop.time()
        return arb.claim("lounge", 0.3, heard_at=heard, slack_s=3.0)
    return run(main())


def test_mixed_fleet_late_controller_wake_cedes():
    assert _mixed_pair(date_by_capture=True) == "office"


def test_mixed_fleet_timed_at_scoring_answered_twice():
    """What stamping the claim at the end of inference did: the delay read
    as a separate utterance and the second Echo answered too."""
    assert _mixed_pair(date_by_capture=False) == "lounge"


# ── a mixed fleet holds its claims (contest) ────────────────────────────────

HOLD = 0.1


def test_contest_without_hold_is_claim():
    """A fleet that detects one way does not wait."""
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        won = await arb.contest("office", WINDOW, hold_s=0)
        second = await arb.contest("lounge", WINDOW, hold_s=0)
        return won, second, loop.time() - t0
    won, second, elapsed = run(main())
    assert (won, second) == ("office", "office")
    assert elapsed < 0.01


def test_contest_grants_the_claim_heard_first():
    """The 2026-09-24 case: the far Echo detects on the device and arrives
    16ms ahead of the near one, which was scored here and heard earlier."""
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        far = asyncio.create_task(
            arb.contest("vvv", WINDOW, heard_at=t - 0.078, slack_s=3.0, hold_s=HOLD))
        await asyncio.sleep(0.016)
        near = asyncio.create_task(
            arb.contest("15le", WINDOW, heard_at=t - 0.110, slack_s=3.0, hold_s=HOLD))
        return await far, await near
    assert run(main()) == ("15le", "15le")


def test_contest_waits_only_until_hold_after_hearing():
    """The hold runs from when the audio was HEARD, so a claim that already
    spent most of it in flight waits only for the rest."""
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await arb.contest("office", WINDOW, heard_at=t0 - 0.08, hold_s=HOLD)
        return loop.time() - t0
    elapsed = run(main())
    assert 0.01 < elapsed < 0.06


def test_contest_solo_claim_wins_after_the_hold():
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        return await arb.contest("office", WINDOW, heard_at=t, hold_s=HOLD)
    assert run(main()) == "office"


def test_claim_after_the_contest_meets_the_granted_winner():
    """Never revoked: a claim heard earlier but arriving after the decision
    still cedes."""
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        first = await arb.contest("office", WINDOW, heard_at=t, slack_s=3.0, hold_s=HOLD)
        late = await arb.contest("lounge", WINDOW, heard_at=t - 0.05, slack_s=3.0, hold_s=HOLD)
        return first, late
    assert run(main()) == ("office", "office")


def test_contest_separate_utterance_opens_its_own():
    """A claim heard outside the open contest's window is another utterance:
    it neither joins nor loses, and deciding one contest leaves the other."""
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        a = asyncio.create_task(arb.contest("office", WINDOW, heard_at=t, hold_s=HOLD))
        await asyncio.sleep(0)
        b = asyncio.create_task(arb.contest("lounge", WINDOW, heard_at=t - 0.5, hold_s=HOLD))
        return await a, await b
    assert run(main()) == ("office", "lounge")


# ── The loudest answers (#747) ──────────────────────────────────────────────
#
# What has to be right is the room. Between rooms the lead is 10dB or more;
# inside one the choice is arbitrary and nobody minds, so there is no margin.

from em_arbiter import Claim, pick


def _who(claims):
    won, why = pick(claims)
    return won.device_id, why


def test_the_loudest_wins_over_the_first_to_hear():
    """The reported case: the first to hear scraped in and was far quieter."""
    claims = [Claim(0.000, "far", -73.0), Claim(0.036, "mid", -72.0),
              Claim(0.078, "near", -48.0)]
    assert _who(claims) == ("near", "loudest by 24.0dB")


def test_there_is_no_margin():
    """Three Echoes side by side on 2026-10-06: one word, heard up to 113ms
    apart and within 2.6dB. The frame grid used to pick; the level does."""
    claims = [Claim(0.000, "15le", -57.8), Claim(0.054, "c95", -53.0),
              Claim(0.110, "vvv", -55.6)]
    assert _who(claims) == ("c95", "loudest by 2.6dB")
    close = [Claim(0.000, "a", -55.0), Claim(0.200, "b", -54.9)]
    assert _who(close)[0] == "b"


def test_being_first_by_a_long_way_does_not_beat_being_louder():
    """Heard times spread up to 224ms for one word that night. They say
    nothing about who is nearer."""
    claims = [Claim(0.000, "far", -75.0), Claim(0.240, "near", -50.0)]
    assert _who(claims)[0] == "near"


def test_a_missing_level_sends_the_whole_contest_back_to_time():
    """Older firmware sends none, and half a comparison is not one."""
    claims = [Claim(0.000, "old", None), Claim(0.050, "new", -40.0)]
    who, why = _who(claims)
    assert who == "old" and "level is missing" in why


def test_a_wake_heard_over_playback_is_decided_on_time():
    """Its reading contains the reply or the music the Echo is playing."""
    claims = [Claim(0.000, "quiet", -70.0),
              Claim(0.050, "playing", -45.0, over_playback=True)]
    who, why = _who(claims)
    assert who == "quiet" and "over playback" in why


def test_different_gain_settings_are_not_compared():
    """MICPGA and digital gain are still inside the reading."""
    claims = [Claim(0.000, "a", -70.0, gains=(40, 88)),
              Claim(0.050, "b", -45.0, gains=(59, 88))]
    who, why = _who(claims)
    assert who == "a" and "gain settings differ" in why
    same = [c._replace(gains=(40, 88)) for c in claims]
    assert _who(same)[0] == "b"


def test_one_claim_is_its_own_answer():
    assert _who([Claim(0.0, "only", None)]) == ("only", "only claim")


def test_a_tie_goes_to_the_claim_that_arrived_first():
    claims = [Claim(0.0, "first", -55.0), Claim(0.0, "second", -55.0)]
    assert _who(claims)[0] == "first"


# The fourteen wakes one house heard on several Echoes at once (#747), as
# numbers only: (ms after the first to hear, averaged level dBFS) per Echo, and
# the index of the loudest. Its lead is 9.7dB at the least. First-to-hear
# chose it in eight.
_HOUSE = [
    ([(0, -69.4), (12, -47.9), (46, -73.4), (181, -71.8)], 1),
    ([(0, -75.8), (55, -64.7), (194, -74.4)], 1),
    ([(0, -64.7), (88, -75.5), (128, -74.6), (368, -83.7)], 0),
    ([(0, -77.9), (76, -61.8)], 1),
    ([(0, -62.5), (87, -78.2), (133, -79.5)], 0),
    ([(0, -63.3), (87, -79.6)], 0),
    ([(0, -57.6), (68, -75.3), (296, -78.7)], 0),
    ([(0, -57.1), (64, -76.7), (134, -77.8)], 0),
    ([(0, -63.1), (85, -73.8), (133, -77.5)], 0),
    ([(0, -77.4), (35, -78.3), (77, -55.8), (311, -75.2)], 2),
    ([(0, -58.3), (233, -78.4)], 0),
    ([(0, -74.0), (44, -77.3), (69, -48.2), (135, -84.2)], 2),
    ([(0, -75.3), (165, -64.9)], 1),
    ([(0, -62.7), (143, -76.0)], 0),
]


def test_the_house_that_reported_it_gets_the_loudest_echo_every_time():
    leads = []
    for heard, loudest in _HOUSE:
        claims = [Claim(ms / 1000.0, str(i), lv) for i, (ms, lv) in enumerate(heard)]
        who, why = _who(claims)
        assert who == str(loudest), (heard, why)
        ranked = sorted((lv for _, lv in heard), reverse=True)
        leads.append(ranked[0] - ranked[1])
    assert round(min(leads), 1) == 9.7
    # Under first-to-hear, six of these went to another Echo.
    assert sum(1 for _, loudest in _HOUSE if loudest != 0) == 6


def test_the_one_heard_over_playback_is_left_to_time_for_now():
    """The reported wake itself: the nearest Echo was playing, so its reading
    is not trusted yet and the first to hear still wins. Known, not fixed."""
    claims = [Claim(0.000, "0", -73.0), Claim(0.036, "1", -72.0),
              Claim(0.078, "2", -48.0, over_playback=True), Claim(0.312, "3", -66.0)]
    assert _who(claims)[0] == "0"


def test_a_held_contest_gives_the_turn_to_the_loudest():
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        far = asyncio.create_task(arb.contest(
            "far", WINDOW, heard_at=t, slack_s=3.0, hold_s=HOLD, level=-70.0))
        await asyncio.sleep(0.05)
        near = asyncio.create_task(arb.contest(
            "near", WINDOW, heard_at=t + 0.05, slack_s=3.0, hold_s=HOLD, level=-50.0))
        return await far, await near, arb.last_why
    assert run(main()) == ("near", "near", "loudest by 20.0dB")


def test_louder_room_beats_a_higher_score_heard_earlier():
    """Kitchen −42 dBFS score 0.94, master −79 dBFS score 0.97, master
    first by 0.16s. The kitchen answers."""
    claims = [Claim(0.00, "master", -79.0, score=0.97),
              Claim(0.16, "kitchen", -42.0, score=0.94)]
    assert _who(claims)[0] == "kitchen"


def test_speech_above_the_noise_floor_is_the_loudness():
    """Same shape with floors: kitchen is still further above its floor."""
    claims = [Claim(0.00, "master", -79.0, score=0.97, floor_db=-85.0),
              Claim(0.16, "kitchen", -42.0, score=0.94, floor_db=-60.0)]
    who, why = _who(claims)
    assert who == "kitchen"
    assert "SNR" in why


def test_equal_loudness_breaks_on_wake_score():
    claims = [Claim(0.00, "master", -50.0, score=0.97),
              Claim(0.05, "kitchen", -50.0, score=0.94)]
    who, why = _who(claims)
    assert who == "master"
    assert "wake score" in why


def test_a_held_contest_gives_the_louder_room_the_turn():
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        master = asyncio.create_task(arb.contest(
            "master", 0.3, heard_at=t, hold_s=HOLD, level=-79.0, score=0.97))
        await asyncio.sleep(0.02)
        kitchen = asyncio.create_task(arb.contest(
            "kitchen", 0.3, heard_at=t + 0.16, hold_s=HOLD, level=-42.0, score=0.94))
        return await master, await kitchen
    assert run(main()) == ("kitchen", "kitchen")


def test_a_single_claim_returns_at_the_early_mark():
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        won = await arb.contest("office", WINDOW, hold_s=0.25, early_s=0.08)
        return won, loop.time() - t0
    won, elapsed = run(main())
    assert won == "office"
    assert 0.05 < elapsed < 0.15


def test_two_claims_a_frame_apart_still_go_to_the_louder_dot():
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        first = asyncio.create_task(arb.contest(
            "quiet", WINDOW, hold_s=0.25, early_s=0.08, level=-70))
        await asyncio.sleep(0.025)
        second = asyncio.create_task(arb.contest(
            "loud", WINDOW, hold_s=0.25, early_s=0.08, level=-40))
        won, also = await first, await second
        return won, also, loop.time() - t0
    won, also, elapsed = run(main())
    assert (won, also) == ("loud", "loud")
    assert elapsed > 0.2


def test_a_second_claim_keeps_the_window_open_for_a_louder_one():
    """A claim inside the early mark holds the old window, so 150ms still wins."""
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        first = asyncio.create_task(arb.contest(
            "first", 0.4, hold_s=0.25, early_s=0.08, level=-60))
        await asyncio.sleep(0.02)
        mid = asyncio.create_task(arb.contest(
            "mid", 0.4, hold_s=0.25, early_s=0.08, level=-50))
        await asyncio.sleep(0.13)
        late = asyncio.create_task(arb.contest(
            "loud", 0.4, hold_s=0.25, early_s=0.08, level=-30))
        results = (await first, await mid, await late)
        return results, loop.time() - t0
    results, elapsed = run(main())
    assert results == ("loud", "loud", "loud")
    assert elapsed > 0.2


def test_a_lone_claim_after_the_early_grant_cedes():
    async def main():
        arb = WakeArbiter()
        first = await arb.contest(
            "office", WINDOW, hold_s=0.25, early_s=0.08, level=-50)
        late = await arb.contest(
            "loud", WINDOW, hold_s=0.25, early_s=0.08, level=-20)
        return first, late
    assert run(main()) == ("office", "office")


def test_early_exit_never_waits_longer_than_the_hold():
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await arb.contest("office", WINDOW, hold_s=0.05, early_s=0.5)
        return loop.time() - t0
    elapsed = run(main())
    assert 0.03 < elapsed < 0.12


def test_early_zero_waits_out_the_hold():
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await arb.contest("office", WINDOW, hold_s=0.1, early_s=0)
        return loop.time() - t0
    elapsed = run(main())
    assert 0.08 < elapsed < 0.18


def test_arbitration_early_defaults_to_80ms_and_clamps(monkeypatch):
    import em_listen
    monkeypatch.delenv("EM_ARBITRATION_EARLY_MS", raising=False)
    assert em_listen.arbitration_early() == 0.08
    assert em_listen.arbitration_early({}) == 0.08
    assert em_listen.arbitration_early({"EM_ARBITRATION_EARLY_MS": ""}) == 0.08
    assert em_listen.arbitration_early({"EM_ARBITRATION_EARLY_MS": "0"}) == 0.0
    assert em_listen.arbitration_early({"EM_ARBITRATION_EARLY_MS": "40"}) == 0.04
    assert em_listen.arbitration_early({"EM_ARBITRATION_EARLY_MS": "999"}) == 0.25
    assert em_listen.arbitration_early({"EM_ARBITRATION_EARLY_MS": "nope"}) == 0.08
    import _sources
    body = _sources.def_source("em_controller._claim_wake")
    assert "early_s=em_listen.arbitration_early()" in body


# ── nearest Dot (arrival-aware contest) ─────────────────────────────────────

from em_arbiter import pick_nearest, nearest_close


def _near(claims, offsets=None):
    return pick_nearest(claims, offsets or {})


def test_replay_0704_late_first_claim_loses_to_the_louder_dot():
    """Living Room arrived 566 ms after it heard the word, so the 250 ms
    hold had already expired. Girls and Boys arrived 25 ms and 59 ms later
    and were louder. The loudest wins, inside the 150 ms arrival wait."""
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        living = asyncio.create_task(arb.contest(
            "living", 0.7, heard_at=t0 - 0.566, hold_s=0.35, level=-62.0,
            score=0.846, floor_db=-80.0, mode="nearest", min_wait_s=0.15,
            cap_s=0.6, early_s=0.08))
        await asyncio.sleep(0.025)
        girls = asyncio.create_task(arb.contest(
            "girls", 0.7, heard_at=t0 - 0.500, hold_s=0.35, level=-41.0,
            score=0.986, floor_db=-78.0, mode="nearest", min_wait_s=0.15,
            cap_s=0.6, early_s=0.08))
        await asyncio.sleep(0.034)
        boys = asyncio.create_task(arb.contest(
            "boys", 0.7, heard_at=t0 - 0.480, hold_s=0.35, level=-44.0,
            score=0.984, floor_db=-79.0, mode="nearest", min_wait_s=0.15,
            cap_s=0.6, early_s=0.08))
        results = await living, await girls, await boys
        return results, arb.last_why, loop.time() - t0
    results, why, elapsed = run(main())
    assert results == ("girls", "girls", "girls")
    assert why == "loudest"
    assert elapsed < 0.6
    assert elapsed > 0.1


def test_ties_break_on_score_then_heard_then_serial():
    score = [
        Claim(0.10, "b", -50.0, score=0.80, floor_db=-70.0),
        Claim(0.00, "a", -50.0, score=0.90, floor_db=-70.0),
    ]
    who, why = _near(score)
    assert (who.device_id, why) == ("a", "tie:score")
    heard = [
        Claim(0.20, "later", -50.00, score=0.9, floor_db=-70.0),
        Claim(0.00, "earlier", -50.04, score=0.9, floor_db=-70.0),
    ]
    who, why = _near(heard)
    assert (who.device_id, why) == ("earlier", "tie:heard")
    serial = [
        Claim(0.0, "m", -50.0, score=0.9, floor_db=-70.0),
        Claim(0.0, "a", -50.0, score=0.9, floor_db=-70.0),
    ]
    who, why = _near(serial)
    assert (who.device_id, why) == ("a", "tie:serial")
    again, why2 = _near(list(reversed(serial)))
    assert again.device_id == "a" and why2 == "tie:serial"


def test_a_room_offset_moves_the_winner():
    claims = [Claim(0.0, "near", -40.0, score=0.9, floor_db=-70.0),
              Claim(0.1, "far", -50.0, score=0.9, floor_db=-70.0)]
    assert _near(claims)[0].device_id == "near"
    assert _near(claims, {"far": 20.0})[0].device_id == "far"


def test_old_firmware_without_a_level_falls_back_to_the_earliest():
    claims = [Claim(0.05, "new", -40.0, score=0.99),
              Claim(0.00, "old", None, score=0.5)]
    who, why = _near(claims)
    assert who.device_id == "old" and why == "fallback:no-level"


def test_echo_cases():
    quiet = Claim(0.0, "quiet", -60.0, score=0.9, floor_db=-80.0)
    echo = Claim(0.02, "echo", -20.0, score=0.99, floor_db=-80.0,
                 over_playback=True, playback_ref=-22.0)
    who, why = _near([quiet, echo])
    assert who.device_id == "quiet" and why == "loudest"
    loud_real = echo._replace(level=-40.0, playback_ref=-20.0)
    assert _near([quiet, loud_real])[0].device_id == "echo"
    solo_ok = Claim(0.0, "solo", -20.0, score=0.90, over_playback=True,
                    playback_ref=-21.0)
    who, why = _near([solo_ok])
    assert who.device_id == "solo" and why == "only"
    solo_weak = solo_ok._replace(score=0.40)
    who, why = _near([solo_weak])
    assert who is None and why == "all_echo"
    other = echo._replace(device_id="other")
    who, why = _near([echo, other])
    assert who is None and why == "all_echo"


def test_low_lag_solo_waits_80ms_and_high_lag_waits_150ms():
    async def once(age):
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        won = await arb.contest(
            "office", WINDOW, heard_at=t0 - age, hold_s=0.35, level=-40.0,
            score=0.9, mode="nearest", min_wait_s=0.15, cap_s=0.6, early_s=0.08)
        return won, loop.time() - t0
    won, low = run(once(0.09))
    assert won == "office" and 0.05 < low < 0.12
    won, high = run(once(0.566))
    assert won == "office" and 0.12 < high < 0.22


def test_the_cap_is_600ms_after_arrival():
    async def main():
        arb = WakeArbiter()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await arb.contest(
            "office", WINDOW, heard_at=t0, hold_s=2.0, level=-40.0,
            mode="nearest", min_wait_s=2.0, cap_s=0.6, early_s=None)
        return loop.time() - t0
    elapsed = run(main())
    assert 0.5 < elapsed < 0.75
    assert nearest_close(0.0, 1.0, 0.35, 0.15, 0.6) == 1.15


def test_legacy_and_shadow_keep_todays_winner():
    """Playback still sends the legacy contest back to who heard first.
    Shadow logs the nearest winner and returns the legacy one."""
    async def main(mode):
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        first = asyncio.create_task(arb.contest(
            "first", 0.7, heard_at=t, hold_s=0.1, level=-70.0, score=0.5,
            mode=mode, early_s=None))
        await asyncio.sleep(0.02)
        playing = asyncio.create_task(arb.contest(
            "playing", 0.7, heard_at=t + 0.02, hold_s=0.1, level=-30.0,
            score=0.99, over_playback=True, playback_ref=-80.0,
            mode=mode, early_s=None))
        return (await first, await playing, arb.last_why,
                None if arb.last_nearest is None else arb.last_nearest[0])
    assert run(main("legacy"))[:2] == ("first", "first")
    applied = run(main("shadow"))
    assert applied[0] == "first" and applied[1] == "first"
    assert "over playback" in applied[2]
    assert applied[3] == "playing"


def test_a_claim_after_the_decision_is_a_late_loser():
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        first = await arb.contest(
            "office", 0.7, heard_at=t - 0.566, hold_s=0.35, level=-50.0,
            score=0.9, mode="nearest", min_wait_s=0.05, cap_s=0.6, early_s=None)
        late = await arb.contest(
            "girls", 0.7, heard_at=t - 0.50, hold_s=0.35, level=-20.0,
            score=0.99, mode="nearest", min_wait_s=0.05, cap_s=0.6)
        return first, late, arb.last_record["reason"]
    assert run(main()) == ("office", "office", "late_loser")


def test_a_held_contest_without_levels_still_goes_to_the_first_to_hear():
    async def main():
        arb = WakeArbiter()
        t = asyncio.get_running_loop().time()
        a = asyncio.create_task(arb.contest("a", WINDOW, heard_at=t, hold_s=HOLD))
        await asyncio.sleep(0.03)
        b = asyncio.create_task(arb.contest("b", WINDOW, heard_at=t + 0.03, hold_s=HOLD))
        return await a, await b
    assert run(main()) == ("a", "a")

