"""Copy paired-Dot data into the local app, and refuse an incomplete target."""

import os
import subprocess
from pathlib import Path

import pytest

import em_start

CONTROLLER = Path(__file__).resolve().parents[1]
SCRIPT = CONTROLLER / "tools/migrate_addon_data.sh"
TLS = ("ca.pem", "ca.key", "server.pem", "server.key")


def _fill(directory: Path, db: bytes = b"sqlite-ok") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "echomuse.db").write_bytes(db)
    tls = directory / "tls"
    tls.mkdir(exist_ok=True)
    for name in TLS:
        (tls / name).write_bytes(b"pem-" + name.encode())
    model = directory / "oww_models"
    model.mkdir(exist_ok=True)
    (model / "hey_leo_v6.onnx").write_bytes(b"onnx-model")
    firmware = directory / "firmware"
    firmware.mkdir(exist_ok=True)
    (firmware / "server").write_bytes(b"firmware")


def _ha(tmp_path: Path, *, fail: bool = False) -> tuple[Path, dict[str, str]]:
    log = tmp_path / "ha.log"
    script = tmp_path / "ha"
    code = "exit 1" if fail else "exit 0"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {log}\n"
        f"{code}\n"
    )
    script.chmod(0o755)
    return log, {"HA_CLI": str(script)}


def _run(root: Path, *args: str, env: dict | None = None,
         data_root: bool = True) -> subprocess.CompletedProcess:
    cmd = ["sh", str(SCRIPT), *args]
    if data_root:
        cmd.extend(["--data-root", str(root)])
    merged = os.environ.copy()
    merged.pop("SUPERVISOR_TOKEN", None)
    merged.pop("HA_CLI", None)
    merged.pop("SUPERVISOR_BASE", None)
    if env:
        merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=merged)


def test_check_refuses_an_empty_target(tmp_path):
    (tmp_path / "local_controller").mkdir()
    result = _run(tmp_path, "check")
    assert result.returncode == 1
    assert "refusing to start" in result.stderr
    assert "echomuse.db" in result.stderr


def test_copy_then_check_then_rollback(tmp_path):
    src = tmp_path / "acc06409_controller"
    _fill(src)
    (src / "echomuse.db-wal").write_bytes(b"wal")
    (src / "keep.txt").write_bytes(b"whole-dir")
    log, env = _ha(tmp_path)
    copied = _run(tmp_path, "copy", env=env)
    assert copied.returncode == 0, copied.stderr
    dest = tmp_path / "local_controller"
    assert (dest / "echomuse.db").read_bytes() == b"sqlite-ok"
    assert (dest / "echomuse.db-wal").read_bytes() == b"wal"
    assert (dest / "keep.txt").read_bytes() == b"whole-dir"
    assert (dest / "oww_models" / "hey_leo_v6.onnx").read_bytes() == b"onnx-model"
    assert (dest / "firmware" / "server").read_bytes() == b"firmware"
    for name in TLS:
        assert (dest / "tls" / name).read_bytes().startswith(b"pem-")
    # The original is still there.
    assert (src / "echomuse.db").read_bytes() == b"sqlite-ok"
    assert "apps options acc06409_controller --boot manual" in log.read_text()

    checked = _run(tmp_path, "check")
    assert checked.returncode == 0, checked.stderr
    assert "Safe to start" in checked.stdout
    assert "hey_leo_v6.onnx" in checked.stdout

    again = _run(tmp_path, "copy", env=env)
    assert again.returncode == 1
    assert "--replace" in again.stderr
    # A refused second copy does not change boot again.
    assert log.read_text().count("--boot manual") == 1

    rolled = _run(tmp_path, "rollback", env=env)
    assert rolled.returncode == 0, rolled.stderr
    assert "acc06409_controller" in rolled.stdout
    assert "boot is auto" in rolled.stdout
    assert "apps options acc06409_controller --boot auto" in log.read_text()
    assert (src / "echomuse.db").is_file()
    assert (dest / "echomuse.db").is_file()


def test_copy_refuses_a_missing_source_and_a_broken_rollback(tmp_path):
    (tmp_path / "acc06409_controller").mkdir()
    log, env = _ha(tmp_path)
    copied = _run(tmp_path, "copy", env=env)
    assert copied.returncode == 1
    assert "refusing to start" in copied.stderr
    assert "boot was not changed" in copied.stderr
    assert not (tmp_path / "local_controller" / "echomuse.db").exists()
    assert not log.exists() or log.read_text() == ""

    rolled = _run(tmp_path, "rollback", env=env)
    assert rolled.returncode == 1
    assert "not intact" in rolled.stderr
    assert "boot was not changed" in rolled.stderr


def test_replace_overwrites_a_target_the_fork_already_created(tmp_path):
    _fill(tmp_path / "acc06409_controller", b"original")
    dest = tmp_path / "local_controller"
    _fill(dest, b"minted")
    (dest / "minted-only.txt").write_bytes(b"go")
    _, env = _ha(tmp_path)
    refused = _run(tmp_path, "copy", env=env)
    assert refused.returncode == 1
    replaced = _run(tmp_path, "copy", "--replace", env=env)
    assert replaced.returncode == 0, replaced.stderr
    assert (dest / "echomuse.db").read_bytes() == b"original"
    assert not (dest / "minted-only.txt").exists()


def test_check_requires_the_wake_model_and_firmware(tmp_path):
    dest = tmp_path / "local_controller"
    _fill(dest)
    (dest / "oww_models" / "hey_leo_v6.onnx").unlink()
    missing_model = _run(tmp_path, "check")
    assert missing_model.returncode == 1
    assert "hey_leo_v6.onnx" in missing_model.stderr
    (dest / "oww_models" / "hey_leo_v6.onnx").write_bytes(b"onnx-model")
    (dest / "firmware" / "server").unlink()
    missing_fw = _run(tmp_path, "check")
    assert missing_fw.returncode == 1
    assert "firmware/" in missing_fw.stderr


def test_a_failed_boot_change_does_not_copy(tmp_path):
    _fill(tmp_path / "acc06409_controller")
    _, env = _ha(tmp_path, fail=True)
    copied = _run(tmp_path, "copy", env=env)
    assert copied.returncode == 1
    assert "boot" in copied.stderr
    assert not (tmp_path / "local_controller" / "echomuse.db").exists()


def test_detects_apps_data_ahead_of_addons(tmp_path):
    base = tmp_path / "supervisor"
    apps = base / "apps" / "data" / "acc06409_controller"
    addons = base / "addons" / "data" / "acc06409_controller"
    _fill(apps, b"from-apps")
    _fill(addons, b"from-addons")
    _, env = _ha(tmp_path)
    env["SUPERVISOR_BASE"] = str(base)
    copied = _run(tmp_path, "copy", env=env, data_root=False)
    assert copied.returncode == 0, copied.stderr
    dest = base / "apps" / "data" / "local_controller" / "echomuse.db"
    assert dest.read_bytes() == b"from-apps"
    assert not (base / "addons" / "data" / "local_controller").exists()


def test_detects_addons_when_that_is_the_layout(tmp_path):
    base = tmp_path / "supervisor"
    _fill(base / "addons" / "data" / "acc06409_controller", b"from-addons")
    _, env = _ha(tmp_path)
    env["SUPERVISOR_BASE"] = str(base)
    copied = _run(tmp_path, "copy", env=env, data_root=False)
    assert copied.returncode == 0, copied.stderr
    dest = base / "addons" / "data" / "local_controller" / "echomuse.db"
    assert dest.read_bytes() == b"from-addons"


def test_data_root_overrides_detection(tmp_path):
    base = tmp_path / "supervisor"
    _fill(base / "apps" / "data" / "acc06409_controller", b"from-apps")
    chosen = tmp_path / "chosen"
    _fill(chosen / "acc06409_controller", b"from-flag")
    _, env = _ha(tmp_path)
    env["SUPERVISOR_BASE"] = str(base)
    copied = _run(chosen, "copy", env=env)
    assert copied.returncode == 0, copied.stderr
    assert (chosen / "local_controller" / "echomuse.db").read_bytes() == b"from-flag"
    assert not (base / "apps" / "data" / "local_controller").exists()


def test_startup_refuses_a_copy_without_the_wake_model(tmp_path, monkeypatch, capsys):
    _fill(tmp_path)
    (tmp_path / "oww_models" / "hey_leo_v6.onnx").unlink()
    monkeypatch.setenv("EM_REQUIRE_EXISTING_DATA", "1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    monkeypatch.delenv("TLS_DIR", raising=False)
    with pytest.raises(SystemExit) as caught:
        em_start.ensure_existing_data()
    assert caught.value.code == 1
    assert "hey_leo_v6.onnx" in capsys.readouterr().out


def test_startup_refuses_an_empty_data_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EM_REQUIRE_EXISTING_DATA", "1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    monkeypatch.delenv("TLS_DIR", raising=False)
    with pytest.raises(SystemExit) as caught:
        em_start.ensure_existing_data()
    assert caught.value.code == 1
    err = capsys.readouterr().out
    assert "refusing to start" in err
    assert "echomuse.db" in err


def test_startup_allows_a_copied_data_dir(tmp_path, monkeypatch):
    _fill(tmp_path)
    monkeypatch.setenv("EM_REQUIRE_EXISTING_DATA", "1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    monkeypatch.delenv("TLS_DIR", raising=False)
    em_start.ensure_existing_data()


def test_startup_does_not_require_data_unless_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("EM_REQUIRE_EXISTING_DATA", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "echomuse.db"))
    em_start.ensure_existing_data()


def test_an_empty_file_counts_as_missing(tmp_path, monkeypatch):
    _fill(tmp_path)
    (tmp_path / "tls" / "ca.pem").write_bytes(b"")
    monkeypatch.delenv("TLS_DIR", raising=False)
    missing = em_start.missing_existing_data(str(tmp_path / "echomuse.db"))
    assert any(path.endswith("ca.pem") for path in missing)
