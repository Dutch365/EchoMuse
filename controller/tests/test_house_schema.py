"""
hv-084: the house's own v32 (`turns.fail_reason`) collides with upstream's
v32 (the stock-sound EQ reset, #229). Upstream numbering is kept; the house
column is an ensure-step, and upstream's EQ reset reaches a house v32 DB once,
behind a system_config marker and after a backup beside the DB.

A house DB therefore stays at schema 32, which the previous house controller
(dev-917084c, 32 migrations) still opens after a rollback.
"""

import json
import os
import sqlite3

import pytest

import em_db

MUSIC = [0, 8, 3, 0, -3, -6, 0, 0]
SPEECH = [0, 4, 2, 0, -2, -5, 0, 0]
VER = "SELECT value FROM system_config WHERE key = 'schema_version'"
MARK = "SELECT value FROM system_config WHERE key = 'house_eq_reset_v32'"


def _house_v32_db(path, fleet_cfg, devices):
    """A DB as the house controller up to dev-917084c left it: upstream v1-31,
    then the house v32 (fail_reason), no marker, one turn with a reason."""
    c = sqlite3.connect(path)
    for sql in em_db.MIGRATIONS[:31]:
        c.executescript(sql)
    c.executescript("""
        ALTER TABLE turns ADD COLUMN fail_reason TEXT;
        UPDATE system_config SET value = '32' WHERE key = 'schema_version';
    """)
    c.execute("UPDATE system_config SET value = ? WHERE key = 'global_device_config'",
              (json.dumps(fleet_cfg),))
    for dev_id, cfg in devices.items():
        c.execute("INSERT INTO devices (device_id, config) VALUES (?, ?)",
                  (dev_id, json.dumps(cfg)))
    c.execute("INSERT INTO turns (device_id, ts, outcome, fail_reason) "
              "VALUES ('kitchen', 1.0, 'error', 'no_stt')")
    c.commit()
    c.close()


def _q(path, sql):
    c = sqlite3.connect(path)
    try:
        row = c.execute(sql).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def _cols(path):
    c = sqlite3.connect(path)
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(turns)")]
    finally:
        c.close()


def _fleet(path):
    return json.loads(_q(path, "SELECT value FROM system_config "
                               "WHERE key = 'global_device_config'"))


def _dev(path, dev_id):
    return json.loads(_q(path, f"SELECT config FROM devices WHERE device_id = '{dev_id}'"))


@pytest.fixture(autouse=True)
def _close():
    yield
    em_db.close()


def test_upstream_numbering_is_kept():
    assert len(em_db.MIGRATIONS) == 32
    assert not any("fail_reason" in m for m in em_db.MIGRATIONS)
    assert em_db._MIGRATION_FIXUPS[32] is em_db._fixup_v32


def test_house_v32_db_gets_the_eq_reset_once_and_keeps_its_rows(tmp_path):
    p = str(tmp_path / "em.db")
    _house_v32_db(p, {"eqBands": MUSIC, "eqLoudness": True},
                  {"kitchen": {"eqBands": SPEECH, "duckDb": -12}})
    em_db.init(p)
    em_db.close()

    assert _q(p, VER) == "32"
    assert _q(p, MARK) == "ran"
    assert _fleet(p)["eqBands"] == [0.0] * 8 and _fleet(p)["eqLoudness"] is False
    assert _dev(p, "kitchen") == {"eqBands": [0.0] * 8, "duckDb": -12}
    assert _q(p, "SELECT fail_reason FROM turns") == "no_stt"
    assert _q(p, "SELECT COUNT(*) FROM turns") == 1
    # Backup beside the DB, holding the pre-reset values.
    bak = p + ".pre-v32-house-eq.bak"
    assert os.path.exists(bak)
    assert json.loads(_q(bak, "SELECT config FROM devices"))["eqBands"] == SPEECH
    assert _q(bak, MARK) is None

    # A second start is a no-op: an owner who sets Music again keeps it.
    c = sqlite3.connect(p)
    c.execute("UPDATE devices SET config = ? WHERE device_id = 'kitchen'",
              (json.dumps({"eqBands": MUSIC}),))
    c.commit()
    c.close()
    os.remove(bak)
    em_db.init(p)
    em_db.close()
    assert _dev(p, "kitchen") == {"eqBands": MUSIC}
    assert not os.path.exists(bak)
    assert _q(p, VER) == "32"


def test_fresh_db_reaches_32_with_fail_reason_and_the_marker(tmp_path):
    p = str(tmp_path / "em.db")
    em_db.init(p)
    em_db.close()
    assert _q(p, VER) == "32"
    assert "fail_reason" in _cols(p)
    assert _q(p, MARK) == "upstream"
    assert not os.path.exists(p + ".pre-v32-house-eq.bak")


def test_house_db_below_32_takes_upstreams_v32_and_the_column(tmp_path):
    p = str(tmp_path / "em.db")
    c = sqlite3.connect(p)
    for sql in em_db.MIGRATIONS[:31]:
        c.executescript(sql)
    c.execute("INSERT INTO devices (device_id, config) VALUES ('k', ?)",
              (json.dumps({"eqBands": SPEECH}),))
    c.commit()
    c.close()
    em_db.init(p)
    em_db.close()
    assert _q(p, VER) == "32"
    assert "fail_reason" in _cols(p)
    assert _dev(p, "k")["eqBands"] == [0.0] * 8
    assert _q(p, MARK) == "upstream"


def test_an_upstream_v32_db_is_not_reset_twice(tmp_path):
    """No fail_reason at 32 = upstream's v32 already ran its own reset."""
    p = str(tmp_path / "em.db")
    c = sqlite3.connect(p)
    for sql in em_db.MIGRATIONS:
        c.executescript(sql)
    c.execute("INSERT INTO devices (device_id, config) VALUES ('k', ?)",
              (json.dumps({"eqBands": MUSIC}),))
    c.commit()
    c.close()
    em_db.init(p)
    em_db.close()
    assert "fail_reason" in _cols(p)
    assert _dev(p, "k")["eqBands"] == MUSIC
    assert _q(p, MARK) == "upstream"


def test_no_backup_means_no_reset(tmp_path, monkeypatch):
    p = str(tmp_path / "em.db")
    _house_v32_db(p, {}, {"k": {"eqBands": SPEECH}})
    monkeypatch.delenv("EM_SKIP_DB_BACKUP", raising=False)
    os.mkdir(p + ".pre-v32-house-eq.bak")  # a directory: the backup cannot be written
    with pytest.raises(RuntimeError, match="back up"):
        em_db.init(p)
    em_db.close()
    assert _dev(p, "k")["eqBands"] == SPEECH
    assert _q(p, MARK) is None


def test_the_previous_house_controller_still_opens_it(tmp_path, monkeypatch):
    """dev-917084c knew 32 migrations; its downgrade guard refuses only
    current > len(MIGRATIONS). The house DB stays at 32, so it opens."""
    p = str(tmp_path / "em.db")
    _house_v32_db(p, {}, {})
    em_db.init(p)
    em_db.close()
    assert int(_q(p, VER)) <= 32
