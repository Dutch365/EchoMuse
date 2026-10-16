"""Second-stage wake check: accept, reject, timeout, and who is sent."""

import asyncio
import base64
import io
import wave

import aiohttp

import _sources
import em_wakeverify
from em_wakeverify import Settings


def run(coro):
    return asyncio.run(coro)


def _cfg(**kw):
    base = dict(url="http://checker.local/wake", timeout_s=0.3, on_error="accept")
    base.update(kw)
    return Settings(**base)


def test_parse_defaults_and_clamps():
    off = em_wakeverify.parse_settings({})
    assert off.enabled is False
    assert off.timeout_s == 0.3
    assert off.on_error == "accept"
    tight = em_wakeverify.parse_settings({
        "WAKE_VERIFY_URL": " http://checker.local/wake ",
        "WAKE_VERIFY_TIMEOUT_MS": "10",
        "WAKE_VERIFY_ON_ERROR": "reject",
    })
    assert tight.url == "http://checker.local/wake"
    assert tight.timeout_s == 0.05
    assert tight.on_error == "reject"
    garbage = em_wakeverify.parse_settings({
        "WAKE_VERIFY_TIMEOUT_MS": "nope",
        "WAKE_VERIFY_ON_ERROR": "maybe",
    })
    assert garbage.timeout_s == 0.3
    assert garbage.on_error == "accept"


def test_private_listening_is_not_a_stream():
    assert em_wakeverify.classify(True, True) == "private"
    assert em_wakeverify.classify(False, False) == "private"
    assert em_wakeverify.classify(False, True) == "stream"
    assert em_wakeverify.classify(False, None) == "unknown"


def test_verdict_requires_a_bool():
    assert em_wakeverify.verdict({"accept": True}, on_error="reject") is True
    assert em_wakeverify.verdict({"accept": False}, on_error="accept") is False
    assert em_wakeverify.verdict({"accept": "true"}, on_error="accept") is True
    assert em_wakeverify.verdict({}, on_error="reject") is False
    assert em_wakeverify.verdict(None, on_error="accept") is True


def test_empty_url_accepts_without_a_request():
    async def post(url, payload, timeout_s):
        raise AssertionError("disabled checker must not be called")
    cfg = Settings(url="", timeout_s=0.3, on_error="accept")
    assert run(em_wakeverify.check(
        cfg, device_id="kitchen", score=0.9, rms=0.01, pcm=b"\x00\x00", post=post,
    )) is True


def test_accept_and_reject_and_the_clip_shape():
    pcm = b"\x01\x00" * 80
    seen = {"payloads": []}

    async def post(url, payload, timeout_s):
        seen["url"] = url
        seen["payloads"].append(payload)
        return 200, {"accept": seen.get("answer", True)}

    async def main():
        seen["answer"] = True
        ok = await em_wakeverify.check(
            _cfg(), device_id="kitchen", score=0.94, rms=0.0123456, pcm=pcm, post=post)
        seen["answer"] = False
        no = await em_wakeverify.check(
            _cfg(), device_id="kitchen", score=0.94, rms=0.01, pcm=pcm, post=post)
        return ok, no

    assert run(main()) == (True, False)
    # The accept call is the one with the unrounded RMS; reject overwrites it.
    body = seen["payloads"][0]
    assert body["device_id"] == "kitchen"
    assert body["score"] == 0.94
    assert body["rms"] == 0.012346
    assert body["sample_rate"] == 16000
    raw = base64.b64decode(body["audio_wav_b64"])
    with wave.open(io.BytesIO(raw), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.readframes(wav.getnframes()) == pcm


def test_timeout_and_http_error_follow_the_policy():
    async def stall(url, payload, timeout_s):
        raise asyncio.TimeoutError

    async def down(url, payload, timeout_s):
        return 503, None

    async def main():
        return (
            await em_wakeverify.check(
                _cfg(), device_id="kitchen", score=0.5, rms=0.0, pcm=b"", post=stall),
            await em_wakeverify.check(
                _cfg(on_error="reject"), device_id="kitchen", score=0.5, rms=0.0,
                pcm=b"", post=stall),
            await em_wakeverify.check(
                _cfg(), device_id="kitchen", score=0.5, rms=0.0, pcm=b"", post=down),
            await em_wakeverify.check(
                _cfg(on_error="reject"), device_id="kitchen", score=0.5, rms=0.0,
                pcm=b"", post=down),
        )

    assert run(main()) == (True, False, True, False)


def test_begin_skips_private_and_a_disabled_checker():
    async def main():
        disabled = em_wakeverify.begin(
            device_id="kitchen", score=0.9, rms=0.01, pcm=b"\x00\x00",
            streaming=True, cfg=Settings())
        private = em_wakeverify.begin(
            device_id="kitchen", score=0.9, rms=0.01, pcm=b"\x00\x00",
            streaming=False, has_session=True, cfg=_cfg())
        return disabled, private, await em_wakeverify.finish(None)

    assert run(main()) == (None, None, True)


def test_errors_and_a_bad_body_follow_the_policy():
    async def broken(url, payload, timeout_s):
        raise aiohttp.ClientError

    async def exploded(url, payload, timeout_s):
        raise RuntimeError("checker down")

    async def nonsense(url, payload, timeout_s):
        return 200, {"accept": "yes"}

    async def main():
        return (
            await em_wakeverify.check(
                _cfg(), device_id="kitchen", score=0.5, rms=0.0, pcm=b"", post=broken),
            await em_wakeverify.check(
                _cfg(on_error="reject"), device_id="kitchen", score=0.5, rms=0.0,
                pcm=b"", post=exploded),
            await em_wakeverify.check(
                _cfg(on_error="reject"), device_id="kitchen", score=0.5, rms=0.0,
                pcm=b"", post=nonsense),
        )

    assert run(main()) == (True, False, False)


def test_post_json_reads_the_checker():
    from aiohttp import web

    async def handler(request):
        body = await request.json()
        assert body["device_id"] == "kitchen"
        return web.json_response({"accept": True})

    async def main():
        app = web.Application()
        app.router.add_post("/wake", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            status, body = await em_wakeverify.post_json(
                f"http://127.0.0.1:{port}/wake",
                {"device_id": "kitchen"}, 1.0)
        finally:
            await runner.cleanup()
        return status, body

    assert run(main()) == (200, {"accept": True})


def test_cancel_and_a_failed_task():
    async def hang():
        await asyncio.Event().wait()

    async def boom():
        raise RuntimeError("checker failed")

    async def main():
        hanging = asyncio.create_task(hang())
        em_wakeverify.cancel(hanging)
        em_wakeverify.cancel(None)
        failed = asyncio.create_task(boom())
        # settings() is the process default: no URL, accept on error.
        em_wakeverify._cached = None
        return await em_wakeverify.finish(failed)

    assert run(main()) is True


def test_a_private_wake_with_a_device_clip_is_checked():
    pcm = b"\x00\x10" * 160
    seen = {}

    async def post(url, payload, timeout_s):
        seen["pcm_b64"] = payload["audio_wav_b64"]
        seen["rms"] = payload["rms"]
        return 200, {"accept": False}

    async def main():
        skipped = em_wakeverify.begin(
            device_id="kitchen", score=0.9, rms=0.01, pcm=b"",
            streaming=False, has_session=True, device_clip=True, cfg=_cfg(), post=post)
        off = em_wakeverify.begin(
            device_id="kitchen", score=0.9, rms=0.01, pcm=pcm,
            streaming=False, has_session=True, device_clip=True,
            cfg=Settings(), post=post)
        task = em_wakeverify.begin(
            device_id="kitchen", score=0.91, rms=em_wakeverify.pcm_rms(pcm), pcm=pcm,
            streaming=False, has_session=True, device_clip=True, cfg=_cfg(), post=post)
        assert skipped is None and off is None and task is not None
        return await em_wakeverify.finish(task)

    assert run(main()) is False
    assert seen["pcm_b64"]
    # A constant sample has a known RMS. 0x1000 / 32768.
    assert abs(em_wakeverify.pcm_rms(b"\x00\x10" * 4) - (0x1000 / 32768)) < 1e-9
    assert em_wakeverify.pcm_rms(b"") == 0.0


def test_device_clip_replaces_the_controller_ring_only_for_a_device_wake():
    ring = b"\x01\x00"
    clip = b"\x02\x00" * 8
    assert em_wakeverify.pcm_for_check(ring, {"preroll": clip}, "device") == clip
    assert em_wakeverify.pcm_for_check(ring, {"preroll": clip}, "controller") == ring
    assert em_wakeverify.pcm_for_check(ring, {"preroll": b""}, "device") == ring
    assert em_wakeverify.pcm_for_check(ring, None, "device") == ring


def test_begin_posts_a_streaming_wake():
    async def post(url, payload, timeout_s):
        assert payload["device_id"] == "kitchen"
        return 200, {"accept": False}

    async def main():
        task = em_wakeverify.begin(
            device_id="kitchen", score=0.94, rms=0.02, pcm=b"\x00\x00",
            streaming=True, cfg=_cfg(), post=post)
        assert task is not None
        return await em_wakeverify.finish(task)

    assert run(main()) is False


def test_a_private_wake_is_checked_before_the_ack():
    """listen_ack is what plays the wake sound. A reject must not beep.

    The check starts before arbitration and is awaited before the ack. A
    cede or a missing HA connection returns first, and the in-flight check
    is cancelled on the way out.
    """
    body = _sources.def_source("em_controller._private_wake_turn")
    code = _sources.code_only(body)
    begin = code.index("em_wakeverify.begin(")
    ack = code.index("await device.listen_ack(session)")
    finish = code.index("await em_wakeverify.finish(verify)")
    assert begin < code.index("_claim_wake(")
    assert finish < ack
    assert "device_clip=" in code
    assert code.index("em_wakeverify.cancel(verify)") > ack


def test_only_an_explicit_accept_unlocks_a_near_miss():
    """A timeout that still wakes, and a reject, leave the speech gate closed."""
    async def accept(url, payload, timeout_s):
        return 200, {"accept": True}

    async def reject(url, payload, timeout_s):
        return 200, {"accept": False}

    async def slow(url, payload, timeout_s):
        raise asyncio.TimeoutError

    async def bad(url, payload, timeout_s):
        return 200, {"accept": "yes"}

    async def once(post, device_id):
        task = em_wakeverify.begin(
            device_id=device_id, score=0.9, rms=0.02, pcm=b"\x00\x00",
            streaming=True, cfg=_cfg(), post=post)
        woke = await em_wakeverify.finish(task)
        return woke, em_wakeverify.consume_accept(device_id)

    async def main():
        accepted = await once(accept, "kitchen")
        again = em_wakeverify.consume_accept("kitchen")
        rejected = await once(reject, "lounge")
        timed = await once(slow, "hall")
        fallback = await once(bad, "den")
        off = em_wakeverify.begin(
            device_id="off", score=0.9, rms=0.02, pcm=b"\x00\x00",
            streaming=True, cfg=Settings())
        return accepted, again, rejected, timed, fallback, off, em_wakeverify.consume_accept("off")

    accepted, again, rejected, timed, fallback, off, off_accept = run(main())
    assert accepted == (True, True)
    assert again is False
    assert rejected == (False, False)
    assert timed[0] is True and timed[1] is False
    assert fallback[0] is True and fallback[1] is False
    assert off is None and off_accept is False


def test_bind_turn_clears_the_mark_and_a_button_drops_the_accept():
    class Dev:
        device_id = "bind-dev"
        gate_near_miss = True

    dev = Dev()
    assert em_wakeverify.bind_turn(dev, True) == ""
    assert dev.gate_near_miss is False

    em_wakeverify._gen["bind-dev"] = 3
    em_wakeverify._accept_gen["bind-dev"] = 3
    dev.gate_near_miss = True
    assert em_wakeverify.bind_turn(dev, False) == ""
    assert dev.gate_near_miss is False
    assert em_wakeverify.consume_accept("bind-dev") is False


def test_cancel_drops_an_accept_recorded_for_that_wake():
    async def hang(url, payload, timeout_s):
        await asyncio.Event().wait()

    async def main():
        task = em_wakeverify.begin(
            device_id="cancel-dev", score=0.9, rms=0.02, pcm=b"\x00\x00",
            streaming=True, cfg=_cfg(), post=hang)
        em_wakeverify._accept_gen["cancel-dev"] = em_wakeverify._gen["cancel-dev"]
        em_wakeverify.cancel(task)
        try:
            await task
        except asyncio.CancelledError:
            pass
        return task.cancelled(), em_wakeverify.consume_accept("cancel-dev")

    assert run(main()) == (True, False)


def test_streaming_wake_is_checked_while_arbitration_holds():
    code = _sources.code_only(_sources.def_source("em_controller._stream_listen"))
    begin = code.index("em_wakeverify.begin(")
    claim = code.index("_claim_wake(")
    finish = code.index("em_wakeverify.finish(verify)")
    assert begin < claim < finish


def test_every_on_device_wake_logs_its_preroll_length():
    """An info line, on every oww_wake, with the clip length in milliseconds.

    0 is a wake that brought no clip. The line is how a running Dot shows
    the buffer arrived, with the checker left off.
    """
    src = _sources.def_source("em_controller._on_oww_wake")
    line = next(l for l in src.splitlines() if "wake pre-roll" in l)
    assert "log.info" in line
    assert "preroll_ms" in line
