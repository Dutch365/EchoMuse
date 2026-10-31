"""
Internal wake-word install for another Home Assistant add-on.

POST /api/internal/oww_install is the only route that accepts
internal_install_secret. It is not an admin session and it is not
ingress. The gates in `_refuse_before_body` run before the body is read.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile

from aiohttp import web

import em_db as db
import em_device_config
import em_http
import em_internal_install as gate
import em_oww_check
import em_oww_models
import em_oww_sync
import em_state

log = logging.getLogger("echomuse.api")

_MAX_DEVICES = 32
_DEVICES_FIELD_MAX = 65536


def register(app: web.Application) -> None:
    app.router.add_post("/api/internal/oww_install", _post_internal_oww_install)


def _refuse_before_body(request: web.Request) -> web.Response | None:
    """403 or 404, or None when the body may be read."""
    peer = request.remote
    if not gate.peer_in_ha_net(peer):
        log.info("internal oww install refused: peer")
        return em_http.error("forbidden", "Forbidden", 403)
    if gate.via_ingress(peer, request.headers.get("X-Ingress-Path")):
        log.info("internal oww install refused: ingress")
        return em_http.error("forbidden", "Forbidden", 403)
    secret = gate.configured_secret()
    if not gate.secret_configured(secret):
        log.info("internal oww install disabled")
        return em_http.error("not_found", "Not found", 404)
    presented = request.headers.get(gate.SECRET_HEADER, "")
    if not gate.secret_matches(presented, secret):
        log.info("internal oww install refused: secret")
        return em_http.error("forbidden", "Forbidden", 403)
    return None


async def _read_capped(field, limit: int) -> bytes | None:
    """The part body, or None when it grows past `limit`."""
    chunks = []
    total = 0
    while True:
        block = await field.read_chunk(64 * 1024)
        if not block:
            return b"".join(chunks)
        total += len(block)
        if total > limit:
            return None
        chunks.append(block)


async def _multipart(request: web.Request):
    """(error response or None, filename, model bytes, devices JSON text)."""
    reader = await request.multipart()
    filename = None
    data = None
    devices_raw = None
    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name == "model" and data is None:
            filename = field.filename or ""
            data = await _read_capped(field, em_oww_models.MAX_MODEL_BYTES)
            if data is None:
                return em_http.error("too_large", "Model exceeds 20 MB limit", 413), None, None, None
        elif field.name == "devices" and devices_raw is None:
            raw = await _read_capped(field, _DEVICES_FIELD_MAX)
            if raw is None:
                return em_http.error("too_large", "Device list is too large", 413), None, None, None
            devices_raw = raw.decode("utf-8", errors="replace")
        else:
            await field.release()
    return None, filename, data, devices_raw


def _device_ids(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, list) or not value or len(value) > _MAX_DEVICES:
        return None
    if len(set(value)) != len(value):
        return None
    if not all(isinstance(item, str) and item and len(item) <= 64 for item in value):
        return None
    return value


def _snapshot(device_id: str) -> tuple[list, dict]:
    return (
        db.get_device_config_sections(device_id),
        db.get_device_config(device_id),
    )


def _restore(device_id: str, snap: tuple[list, dict]) -> None:
    sections, config = snap
    db.set_device_config_sections(device_id, sections)
    db.set_device_config(device_id, config)


def _point_model(device_id: str, model_path: str) -> None:
    """Store owwModel on this Dot. The asset planner reads that stored value."""
    sections = db.get_device_config_sections(device_id)
    if "wakeword" not in sections:
        db.set_device_config_sections(device_id, [*sections, "wakeword"])
    current = db.get_device_config(device_id)
    db.set_device_config(device_id, {**current, "owwModel": model_path})


def _store_model(fname: str, data: bytes) -> str:
    directory = em_oww_models.models_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        dest = directory / fname
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return str(dest.resolve())


async def _install_one(device_id: str, model_path: str) -> dict:
    """
    Install assets for the stored model, then keep the new owwModel only
    when the read-back matches. A failed install restores the previous config.

    The config write happens before the asset sync because the planner
    reads the stored owwModel. The write is rolled back when the sync or
    the read-back fails, so the Dot stays on its previous model.
    """
    failed = {"id": device_id, "ok": False, "readback_ok": False}
    live = em_state.devices.get(device_id)
    if live is None:
        failed["stage_failed"] = "assets"
        return failed

    loop = asyncio.get_running_loop()
    snap = await loop.run_in_executor(None, _snapshot, device_id)
    try:
        await loop.run_in_executor(None, _point_model, device_id, model_path)
        result = await em_oww_sync.sync(live, device_id)
    except Exception as exc:
        log.info("internal oww install %s assets error: %s", device_id, type(exc).__name__)
        await loop.run_in_executor(None, _restore, device_id, snap)
        failed["stage_failed"] = "assets"
        return failed

    if not result.get("ok"):
        log.info("internal oww install %s assets failed", device_id)
        await loop.run_in_executor(None, _restore, device_id, snap)
        failed["stage_failed"] = "assets"
        return failed

    effective = await loop.run_in_executor(None, db.get_effective_device_config, device_id)
    readback_ok = (effective.get("owwModel") or "") == model_path
    if not readback_ok:
        log.info("internal oww install %s read-back mismatch", device_id)
        await loop.run_in_executor(None, _restore, device_id, snap)
        failed["stage_failed"] = "readback"
        failed["readback_ok"] = False
        return failed

    try:
        await em_device_config.apply_live_config(device_id, live, effective)
    except Exception as exc:
        log.info("internal oww install %s push error: %s", device_id, type(exc).__name__)
        await loop.run_in_executor(None, _restore, device_id, snap)
        failed["stage_failed"] = "push"
        return failed

    log.info("internal oww install %s ok", device_id)
    return {"id": device_id, "ok": True, "readback_ok": True}


async def _post_internal_oww_install(request: web.Request) -> web.Response:
    """
    POST /api/internal/oww_install

    Multipart fields: `model` (.onnx) and `devices` (JSON list of device
    ids). Header X-EM-Install-Secret. Per device the body is
    {id, ok, stage_failed?, readback_ok}.
    """
    refused = _refuse_before_body(request)
    if refused is not None:
        return refused

    err, filename, data, devices_raw = await _multipart(request)
    if err is not None:
        return err
    fname = em_oww_models.safe_model_filename(filename or "")
    if fname is None:
        return em_http.error(
            "invalid_filename",
            "Model must be a .onnx file with a simple name "
            "(letters, digits, _ - . only)",
            400,
        )
    if not data:
        return em_http.error("empty_upload", "Uploaded model is empty", 400)
    ids = _device_ids(devices_raw)
    if ids is None:
        return em_http.error("bad_request", "devices must be a JSON list of device ids", 400)

    loop = asyncio.get_running_loop()

    def _known() -> list[str]:
        return [device_id for device_id in ids if db.get_device(device_id) is None]

    unknown = await loop.run_in_executor(None, _known)
    if unknown:
        return em_http.error("unknown_device", "Unknown device id", 400)

    why = await loop.run_in_executor(None, em_oww_check.problem, data)
    if why:
        log.info("internal oww install model refused: %s", fname)
        return em_http.error(
            "invalid_model",
            "This file cannot run as a wake word model.",
            400,
        )

    try:
        path = await loop.run_in_executor(None, _store_model, fname, data)
    except Exception as exc:
        log.info("internal oww install store error: %s", type(exc).__name__)
        return em_http.error("upload_failed", "Could not store the model", 500)

    log.info("internal oww install stored %s for %d devices", fname, len(ids))
    devices = [await _install_one(device_id, path) for device_id in ids]
    return em_http.ok({"model": {"file": fname, "path": path}, "devices": devices})
