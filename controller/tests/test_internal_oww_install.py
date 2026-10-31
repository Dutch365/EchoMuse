"""
POST /api/internal/oww_install — one add-on on the Home Assistant network,
one shared secret, one model, named Dots.

The secret is a test value. Peer addresses outside 172.30.32.0/23 are
computed from that network so the suite does not carry another address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from unittest.mock import Mock
import pytest
from aiohttp import MultipartWriter
from aiohttp.http import HttpVersion
from aiohttp.http_parser import RawRequestMessage
from aiohttp.streams import StreamReader
from aiohttp.web_request import Request
from multidict import CIMultiDict
from yarl import URL

import em_db
import em_ingressauth
import em_internal_install as gate
import em_routes_internal
import em_routes_oww
import em_state
import _sources

SECRET = "test-install-secret"
INSIDE = "172.30.33.9"
PATH = "/api/internal/oww_install"


def _outside() -> str:
    net = ipaddress.ip_network("172.30.32.0/23")
    return str(ipaddress.ip_address(int(net.network_address) - 1))


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "em.db"))
    em_db.init(str(tmp_path / "em.db"))
    em_db._conn.execute(
        "INSERT INTO devices (device_id, label, approved) VALUES ('DOT-A', 'a', 1)"
    )
    em_db._conn.execute(
        "INSERT INTO devices (device_id, label, approved) VALUES ('DOT-B', 'b', 1)"
    )
    em_db._conn.commit()


class _Sink:
    def __init__(self):
        self.buf = bytearray()

    async def write(self, data):
        self.buf.extend(data)


async def _form(model: bytes, devices, filename="hey_custom.onnx"):
    writer = MultipartWriter("form-data")
    part = writer.append(model)
    part.set_content_disposition("form-data", name="model", filename=filename)
    listed = writer.append(json.dumps(devices))
    listed.set_content_disposition("form-data", name="devices")
    sink = _Sink()
    await writer.write(sink)
    return bytes(sink.buf), writer.headers["Content-Type"]


def _request(body: bytes, headers: dict, remote: str) -> Request:
    loop = asyncio.get_running_loop()
    protocol = Mock()
    protocol.max_field_size = 8190
    protocol.max_headers = 128
    payload = StreamReader(protocol, 2 ** 20, loop=loop)
    if body:
        payload.feed_data(body)
    payload.feed_eof()
    msg = RawRequestMessage(
        "POST", PATH, HttpVersion(1, 1), CIMultiDict(headers),
        (), False, False, False, False, URL(PATH),
    )
    return Request(
        msg, payload, protocol, Mock(), Mock(), loop,
        remote=remote, client_max_size=8 * 1024 * 1024,
    )


class _Unread(StreamReader):
    def __init__(self, loop):
        protocol = Mock()
        super().__init__(protocol, 2 ** 16, loop=loop)
        self.protocol = protocol

    async def read(self, n=-1):
        raise AssertionError("body was read")

    async def readline(self):
        raise AssertionError("body was read")

    def read_nowait(self, n=-1):
        raise AssertionError("body was read")


def _closed_request(headers: dict, remote: str) -> Request:
    """A request whose body raises if anything reads it."""
    loop = asyncio.get_running_loop()
    payload = _Unread(loop)
    payload.protocol.max_field_size = 8190
    payload.protocol.max_headers = 128
    msg = RawRequestMessage(
        "POST", PATH, HttpVersion(1, 1), CIMultiDict(headers),
        (), False, False, False, False, URL(PATH),
    )
    return Request(
        msg, payload, payload.protocol, Mock(), Mock(), loop,
        remote=remote, client_max_size=8 * 1024 * 1024,
    )


async def _call(request: Request):
    response = await em_routes_internal._post_internal_oww_install(request)
    text = response.text or ""
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None
    return response.status, parsed, text


def _headers(secret: str | None = SECRET, extra=None):
    headers = {}
    if secret is not None:
        headers[gate.SECRET_HEADER] = secret
    if extra:
        headers.update(extra)
    return headers


def test_gates_do_not_trust_a_forwarding_header():
    assert gate.peer_in_ha_net(INSIDE)
    assert gate.peer_in_ha_net("172.30.32.1")
    assert gate.peer_in_ha_net("172.30.33.255")
    assert not gate.peer_in_ha_net(_outside())
    assert not gate.peer_in_ha_net(None)
    assert not gate.peer_in_ha_net("not-an-ip")
    assert gate.via_ingress(em_ingressauth.INGRESS_GATEWAY_IP, None)
    assert gate.via_ingress(INSIDE, "/api/hassio_ingress/token")
    assert not gate.via_ingress(INSIDE, None)
    assert not gate.via_ingress(INSIDE, "  ")
    assert gate.secret_matches(SECRET, SECRET)
    assert not gate.secret_matches(SECRET, SECRET + "x")
    assert not gate.secret_matches("", SECRET)
    assert not gate.secret_configured("")
    assert not gate.secret_configured("   ")
    assert gate.secret_configured(SECRET)


def test_the_handler_refuses_before_reading_the_body():
    body = _sources.def_source("_post_internal_oww_install")
    assert body.index("_refuse_before_body") < body.index("_multipart")
    refuse = _sources.def_source("_refuse_before_body")
    assert "multipart" not in refuse
    assert "X-Forwarded-For" not in body
    assert "X-Forwarded-For" not in refuse
    assert "compare_digest" not in body  # the compare lives in one place
    assert "hmac.compare_digest" in _sources.def_source("secret_matches")
    assert "require_admin" not in body


def test_admin_upload_is_still_a_session(monkeypatch):
    monkeypatch.setenv(gate.SECRET_ENV, SECRET)
    upload = _sources.find_def("_post_oww_model_upload")[1]
    names = {d.attr for d in upload.decorator_list if hasattr(d, "attr")}
    assert "require_admin" in names

    async def run():
        request = _closed_request(
            _headers(extra={"X-Forwarded-For": INSIDE}), INSIDE)
        response = await em_routes_oww._post_oww_model_upload(request)
        return response.status

    assert asyncio.run(run()) == 401


def test_wrong_missing_and_empty_secret(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    async def run():
        monkeypatch.setenv(gate.SECRET_ENV, SECRET)
        wrong = await _call(_closed_request(_headers("nope"), INSIDE))
        missing = await _call(_closed_request(_headers(None), INSIDE))
        monkeypatch.delenv(gate.SECRET_ENV, raising=False)
        disabled = await _call(_closed_request(_headers(SECRET), INSIDE))
        blank = await _call(_closed_request(_headers(SECRET), INSIDE))
        monkeypatch.setenv(gate.SECRET_ENV, "   ")
        spaces = await _call(_closed_request(_headers(SECRET), INSIDE))
        return wrong, missing, disabled, blank, spaces

    # blank uses the deleted env from the previous step; re-run spaces after set.
    wrong, missing, disabled, blank, spaces = asyncio.run(run())
    assert wrong[0] == 403 and wrong[1]["code"] == "forbidden"
    assert missing[0] == 403
    assert disabled[0] == 404
    assert blank[0] == 404
    assert spaces[0] == 404
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage()


def test_outside_peer_and_spoofed_forwarding_header(monkeypatch, caplog):
    monkeypatch.setenv(gate.SECRET_ENV, SECRET)
    caplog.set_level(logging.DEBUG)
    outside = _outside()
    forwarded = {"X-Forwarded-For": INSIDE, "X-Real-IP": INSIDE}

    async def run():
        spoofed = await _call(_closed_request(_headers(extra=forwarded), outside))
        bare = await _call(_closed_request(_headers(), outside))
        return spoofed, bare

    spoofed, bare = asyncio.run(run())
    assert spoofed[0] == 403 and bare[0] == 403
    assert SECRET not in spoofed[2] and SECRET not in bare[2]
    assert outside not in caplog.text
    assert SECRET not in caplog.text


def test_ingress_is_refused(monkeypatch):
    monkeypatch.setenv(gate.SECRET_ENV, SECRET)

    async def run():
        gateway = await _call(_closed_request(
            _headers(), em_ingressauth.INGRESS_GATEWAY_IP))
        pathed = await _call(_closed_request(
            _headers(extra={"X-Ingress-Path": "/api/hassio_ingress/token"}),
            INSIDE,
        ))
        return gateway, pathed

    gateway, pathed = asyncio.run(run())
    assert gateway[0] == 403
    assert pathed[0] == 403
    assert SECRET not in gateway[2] and SECRET not in pathed[2]


def test_ingress_middleware_lets_only_this_path_through(monkeypatch):
    from aiohttp import web

    import em_api
    import em_http

    monkeypatch.setattr(em_ingressauth, "INGRESS_ONLY", True)
    reached = []

    async def handler(request):
        reached.append(request.path)
        return em_http.ok({"reached": True})

    async def one(request):
        try:
            response = await em_api._ingress_only_middleware(request, handler)
        except web.HTTPException as exc:
            return exc.status
        return response.status

    async def run():
        blocked = await one(_closed_request({}, _outside()))
        allowed = await one(_request(b"", {}, INSIDE))
        other = await one(_request_at("/api/devices", INSIDE))
        return blocked, allowed, other

    blocked, allowed, other = asyncio.run(run())
    assert blocked == 403
    assert allowed == 200
    assert other == 403
    assert reached == [PATH]


def _request_at(path: str, remote: str) -> Request:
    loop = asyncio.get_running_loop()
    protocol = Mock()
    payload = StreamReader(protocol, 2 ** 16, loop=loop)
    payload.feed_eof()
    msg = RawRequestMessage(
        "GET", path, HttpVersion(1, 1), CIMultiDict(),
        (), False, False, False, False, URL(path),
    )
    return Request(msg, payload, protocol, Mock(), Mock(), loop, remote=remote)


def _model_path(device_id: str) -> str:
    return em_db.get_effective_device_config(device_id).get("owwModel") or ""


def test_success_and_per_device_failures(tmp_path, monkeypatch, caplog):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv(gate.SECRET_ENV, SECRET)
    monkeypatch.setattr(em_routes_internal.em_oww_check, "problem", lambda data: None)
    caplog.set_level(logging.DEBUG)

    calls = []

    async def sync(live, device_id):
        calls.append(device_id)
        if device_id == "DOT-B":
            return {"ok": False, "error": "no space"}
        return {"ok": True, "pushed": []}

    async def apply(device_id, live, effective):
        calls.append(("apply", device_id, effective.get("owwModel")))

    monkeypatch.setattr(em_routes_internal.em_oww_sync, "sync", sync)
    monkeypatch.setattr(em_routes_internal.em_device_config, "apply_live_config", apply)
    em_state.devices.clear()
    em_state.devices["DOT-A"] = object()
    em_state.devices["DOT-B"] = object()
    before_b = _model_path("DOT-B")

    async def run():
        body, ctype = await _form(b"not-a-real-onnx", ["DOT-A", "DOT-B"])
        headers = _headers(extra={"Content-Type": ctype})
        return await _call(_request(body, headers, INSIDE))

    status, payload, text = asyncio.run(run())
    assert status == 200, text
    assert payload["model"]["file"] == "hey_custom.onnx"
    by_id = {row["id"]: row for row in payload["devices"]}
    assert by_id["DOT-A"]["ok"] is True
    assert by_id["DOT-A"]["readback_ok"] is True
    assert "stage_failed" not in by_id["DOT-A"]
    assert _model_path("DOT-A") == payload["model"]["path"]
    assert by_id["DOT-B"] == {
        "id": "DOT-B", "ok": False, "stage_failed": "assets", "readback_ok": False,
    }
    assert _model_path("DOT-B") == before_b
    assert calls[0] == "DOT-A"
    assert SECRET not in text
    assert SECRET not in caplog.text
    em_state.devices.clear()


def test_readback_mismatch_restores_the_previous_model(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv(gate.SECRET_ENV, SECRET)
    monkeypatch.setattr(em_routes_internal.em_oww_check, "problem", lambda data: None)

    async def sync(live, device_id):
        return {"ok": True, "pushed": []}

    monkeypatch.setattr(em_routes_internal.em_oww_sync, "sync", sync)
    real = em_routes_internal.db.get_effective_device_config

    def spoil(device_id):
        cfg = dict(real(device_id))
        model = str(cfg.get("owwModel") or "")
        if model.endswith(".onnx") and "/" in model:
            cfg["owwModel"] = "hey_jarvis_v0.1"
        return cfg

    monkeypatch.setattr(em_routes_internal.db, "get_effective_device_config", spoil)
    em_state.devices.clear()
    em_state.devices["DOT-A"] = object()
    before = real("DOT-A").get("owwModel")

    async def run():
        body, ctype = await _form(b"onnx", ["DOT-A"])
        return await _call(_request(body, _headers(extra={"Content-Type": ctype}), INSIDE))

    status, payload, _text = asyncio.run(run())
    assert status == 200
    assert payload["devices"] == [{
        "id": "DOT-A", "ok": False, "stage_failed": "readback", "readback_ok": False,
    }]
    assert real("DOT-A").get("owwModel") == before
    em_state.devices.clear()


def test_unknown_device_does_not_store_a_model(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv(gate.SECRET_ENV, SECRET)
    monkeypatch.setattr(em_routes_internal.em_oww_check, "problem", lambda data: "nope")

    async def run():
        body, ctype = await _form(b"onnx", ["DOT-A", "NO-SUCH"])
        return await _call(_request(body, _headers(extra={"Content-Type": ctype}), INSIDE))

    status, payload, _text = asyncio.run(run())
    assert status == 400 and payload["code"] == "unknown_device"
    assert list(em_routes_internal.em_oww_models.models_dir().glob("*.onnx")) == []


def test_offline_device_is_an_assets_failure(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv(gate.SECRET_ENV, SECRET)
    monkeypatch.setattr(em_routes_internal.em_oww_check, "problem", lambda data: None)
    em_state.devices.clear()
    before = _model_path("DOT-A")

    async def run():
        body, ctype = await _form(b"onnx", ["DOT-A"])
        return await _call(_request(body, _headers(extra={"Content-Type": ctype}), INSIDE))

    status, payload, _text = asyncio.run(run())
    assert status == 200
    assert payload["devices"][0]["stage_failed"] == "assets"
    assert payload["devices"][0]["ok"] is False
    assert _model_path("DOT-A") == before


def test_a_bad_filename_is_refused(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv(gate.SECRET_ENV, SECRET)

    async def run():
        body, ctype = await _form(b"onnx", ["DOT-A"], filename="../../etc/passwd")
        return await _call(_request(body, _headers(extra={"Content-Type": ctype}), INSIDE))

    status, payload, _text = asyncio.run(run())
    assert status == 400 and payload["code"] == "invalid_filename"
