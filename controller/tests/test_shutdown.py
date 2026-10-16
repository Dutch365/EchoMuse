"""SIGTERM has to end the process, because the add-on is PID 1."""

import asyncio
import signal
import subprocess
import sys
from pathlib import Path

import em_db
import em_shutdown

CONTROLLER = Path(__file__).resolve().parents[1]


def test_sigterm_ends_a_waiting_loop():
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import asyncio, em_shutdown\n"
         "async def main():\n"
         "    stop = asyncio.Event()\n"
         "    em_shutdown.install(asyncio.get_running_loop(), stop)\n"
         "    print('READY', flush=True)\n"
         "    await stop.wait()\n"
         "asyncio.run(main())\n"],
        cwd=str(CONTROLLER),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    assert line.strip() == "READY", proc.stderr.read()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=5) == 0


def test_finish_stops_players_listeners_and_the_database():
    calls = []

    async def stop_players():
        calls.append("players")

    async def stop_ble():
        calls.append("ble")

    async def stop_voice():
        calls.append("voice")

    def close_db():
        calls.append("db")

    class Task:
        def cancel(self):
            calls.append("cancel")

    class Azc:
        async def async_unregister_service(self, info):
            calls.append("mdns")

        async def async_close(self):
            calls.append("mdns-close")

    class Runner:
        async def cleanup(self):
            calls.append("runner")

    asyncio.run(em_shutdown.finish(
        (Task(),), Azc(), object(), Runner(),
        deps=(stop_players, stop_ble, stop_voice, close_db),
    ))
    assert calls == [
        "players", "ble", "voice", "cancel", "mdns", "mdns-close",
        "runner", "db",
    ]


def test_close_checkpoints_and_a_second_close_is_quiet(tmp_path):
    path = tmp_path / "em.db"
    em_db.init(str(path))
    em_db.close()
    assert em_db._conn is None
    em_db.close()
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        version = conn.execute(
            "SELECT value FROM system_config WHERE key = 'schema_version'"
        ).fetchone()[0]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(turns)")]
    finally:
        conn.close()
    assert version == "32"
    assert "fail_reason" in cols
