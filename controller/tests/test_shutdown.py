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


def test_the_handler_sets_the_stop_event_for_a_real_or_unknown_signal():
    stop = asyncio.Event()
    em_shutdown._on_signal(stop, signal.SIGTERM)
    assert stop.is_set()
    other = asyncio.Event()
    em_shutdown._on_signal(other, 99999)
    assert other.is_set()


def test_install_registers_on_a_running_loop_and_survives_a_refusal():
    seen = []

    class Loop:
        def add_signal_handler(self, sig, cb, *args):
            seen.append(sig)
            if sig == signal.SIGINT:
                raise RuntimeError("not the main thread")

    em_shutdown.install(Loop(), asyncio.Event())
    assert signal.SIGTERM in seen

    async def on_a_real_loop():
        loop = asyncio.get_running_loop()
        em_shutdown.install(loop, asyncio.Event())
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

    asyncio.run(on_a_real_loop())


def test_finish_imports_the_real_stoppers(monkeypatch):
    import em_db
    monkeypatch.setattr(em_db, "close", lambda: None)

    class Azc:
        async def async_unregister_service(self, info):
            raise OSError("mdns down")

        async def async_close(self):
            pass

    class Runner:
        async def cleanup(self):
            pass

    asyncio.run(em_shutdown.finish((), Azc(), object(), Runner()))


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
