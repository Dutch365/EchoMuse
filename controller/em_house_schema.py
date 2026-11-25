"""
House schema steps (hv-084).

This fork shipped `turns.fail_reason` as its own migration v32 before
upstream used v32 for the stock-sound EQ reset (#229). Rather than renumber
upstream's list (and every later catch-up with it), the house column is an
idempotent ensure-step and the EQ reset reaches house DBs once, behind a
system_config marker, after a backup beside the DB. The schema version stays
upstream's, so a house DB stays at 32 and the previous house controller
(dev-917084c, which knows 32 migrations) still opens it after a rollback.

Called by em_db._migrate after the upstream migrations, on every start.
"""

import logging
import os
import sqlite3

log = logging.getLogger("echomuse.db")

EQ_RESET_KEY = "house_eq_reset_v32"


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return False
    return any(r[1] == column for r in rows)


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Add `turns.fail_reason` if missing. NULL = did not fail, or unknown."""
    if has_column(conn, "turns", "fail_reason"):
        return
    conn.execute("ALTER TABLE turns ADD COLUMN fail_reason TEXT")
    conn.commit()
    log.info("[db] Added turns.fail_reason (house)")


def _backup(conn: sqlite3.Connection, db_path: str) -> None:
    """Same contract as em_db._backup_before_migrating: no backup, no change."""
    if os.environ.get("EM_SKIP_DB_BACKUP") == "1":
        log.warning("[db] EM_SKIP_DB_BACKUP=1 — EQ reset without a backup")
        return
    dest = f"{db_path}.pre-v32-house-eq.bak"
    try:
        with sqlite3.connect(dest) as bck:
            conn.backup(bck)
        log.info(f"[db] Backed up before the house EQ reset to {dest}")
    except Exception as e:
        raise RuntimeError(
            f"Could not back up the database before the EQ reset ({e}). "
            f"Refusing to change it: free some space or fix permissions on "
            f"{os.path.dirname(db_path) or '.'}, or set EM_SKIP_DB_BACKUP=1."
        ) from e


def eq_reset(conn: sqlite3.Connection, db_path: str, fixup, pending: bool) -> None:
    """
    Run upstream's v32 EQ reset (`fixup`) once on a DB that reached 32
    through the house's v32. `pending` False means the DB took upstream's
    v32 itself (fresh, or migrated through it), so the reset already ran and
    only the marker is written.
    """
    row = conn.execute("SELECT value FROM system_config WHERE key = ?",
                       (EQ_RESET_KEY,)).fetchone()
    if row is not None:
        return
    if pending:
        _backup(conn, db_path)
        log.info("[db] Applying upstream v32 EQ reset to a house v32 DB")
    try:
        if pending:
            fixup(conn)
        conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
                     (EQ_RESET_KEY, "ran" if pending else "upstream"))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise RuntimeError("House EQ reset (upstream v32) failed — cannot start") from e


def after_migrate(conn: sqlite3.Connection, db_path: str, fixup, eq_reset_pending: bool) -> None:
    ensure_columns(conn)
    eq_reset(conn, db_path, fixup, eq_reset_pending)
