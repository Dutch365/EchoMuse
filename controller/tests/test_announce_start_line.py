"""
Announcement start line (#155): one log line per Dot per announcement with the
first-frame wall time and the device's primeWaitMs, so the start spread across
Dots can be read from the logs with no firmware change.
"""

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import em_startspread

CONTROLLER = Path(__file__).resolve().parent.parent


def test_line_has_first_frame_prime_wait_and_audible_start():
    line = em_startspread.announce_start_line(
        1_763_517_723_123, {"primeWaitMs": 412, "minDepth": 3}, 5230)
    assert line == ("Announce start: first_frame_ms=1763517723123 "
                    "prime_wait_ms=412 audible_ms=1763517723535 delivery_ms=5230")


def test_older_firmware_without_stats_does_not_guess_zero():
    line = em_startspread.announce_start_line(1000, {}, 900)
    assert "prime_wait_ms=none" in line
    assert "audible_ms" not in line
    assert em_startspread.announce_start_line(1000, None) == \
        "Announce start: first_frame_ms=1000 prime_wait_ms=none"


def test_bad_prime_values_are_not_used():
    for bad in (True, -5, "412", None):
        line = em_startspread.announce_start_line(1000, {"primeWaitMs": bad})
        assert "audible_ms" not in line, bad


def test_zero_prime_wait_is_kept():
    line = em_startspread.announce_start_line(1000, {"primeWaitMs": 0}, -1)
    assert line == "Announce start: first_frame_ms=1000 prime_wait_ms=0 audible_ms=1000"


def _playback_stats_source():
    tree = ast.parse((CONTROLLER / "em_controller.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_on_playback_stats")
    return ast.unparse(fn)


def test_logged_only_for_streams_with_no_turn():
    src = _playback_stats_source()
    assert "em_startspread.announce_start_line(" in src
    guard = src.index("if turn_id is None and first_frame_ms is not None")
    assert guard < src.index("em_startspread.announce_start_line(")


def test_first_frame_wall_time_is_taken_before_the_window_is_cleared():
    src = _playback_stats_source()
    assert src.index("first_frame_ms = int(time.time() * 1000) - delivery_ms") < \
        src.index("device.playback_send_t0 = None")
