"""Copy paired-Dot data into the local add-on, and refuse an empty target."""

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


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(SCRIPT), *args, "--data-root", str(root)],
        capture_output=True, text=True)


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
    copied = _run(tmp_path, "copy")
    assert copied.returncode == 0, copied.stderr
    dest = tmp_path / "local_controller"
    assert (dest / "echomuse.db").read_bytes() == b"sqlite-ok"
    assert (dest / "echomuse.db-wal").read_bytes() == b"wal"
    for name in TLS:
        assert (dest / "tls" / name).read_bytes().startswith(b"pem-")
    # The original is still there.
    assert (src / "echomuse.db").read_bytes() == b"sqlite-ok"

    checked = _run(tmp_path, "check")
    assert checked.returncode == 0, checked.stderr
    assert "Safe to start" in checked.stdout

    again = _run(tmp_path, "copy")
    assert again.returncode == 1
    assert "--replace" in again.stderr

    rolled = _run(tmp_path, "rollback")
    assert rolled.returncode == 0, rolled.stderr
    assert "acc06409_controller" in rolled.stdout
    assert (src / "echomuse.db").is_file()
    assert (dest / "echomuse.db").is_file()


def test_copy_refuses_a_missing_source_and_a_broken_rollback(tmp_path):
    (tmp_path / "acc06409_controller").mkdir()
    copied = _run(tmp_path, "copy")
    assert copied.returncode == 1
    assert "refusing to start" in copied.stderr
    assert not (tmp_path / "local_controller" / "echomuse.db").exists()

    rolled = _run(tmp_path, "rollback")
    assert rolled.returncode == 1
    assert "not intact" in rolled.stderr


def test_replace_overwrites_a_target_the_fork_already_created(tmp_path):
    _fill(tmp_path / "acc06409_controller", b"original")
    _fill(tmp_path / "local_controller", b"minted")
    refused = _run(tmp_path, "copy")
    assert refused.returncode == 1
    replaced = _run(tmp_path, "copy", "--replace")
    assert replaced.returncode == 0, replaced.stderr
    assert (tmp_path / "local_controller" / "echomuse.db").read_bytes() == b"original"


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
