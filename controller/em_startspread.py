"""
One log line per Dot per announcement, for measuring start spread (#155).

The device already reports `primeWaitMs` (how long it held the first frames
before starting the speaker) in `playback_stats` at the end of every stream.
For announcements nothing kept it. First-frame-sent wall time plus
`primeWaitMs` is when the Dot actually started playing, so comparing that
number across Dots for the same announcement gives the start spread with no
firmware change.

Pure formatting only: no I/O, no timing, nothing on the audio path.
"""


def announce_start_line(first_frame_ms, pstats, delivery_ms=-1) -> str:
    """Format the start line. `first_frame_ms` is epoch milliseconds.

    `audible_ms` is left out when the firmware sent no primeWaitMs
    (older devices omit the stats block), rather than guessing 0.
    """
    first = int(first_frame_ms)
    prime = (pstats or {}).get("primeWaitMs")
    parts = [f"first_frame_ms={first}"]
    if isinstance(prime, (int, float)) and not isinstance(prime, bool) and prime >= 0:
        parts.append(f"prime_wait_ms={int(prime)}")
        parts.append(f"audible_ms={first + int(prime)}")
    else:
        parts.append("prime_wait_ms=none")
    if delivery_ms is not None and delivery_ms >= 0:
        parts.append(f"delivery_ms={int(delivery_ms)}")
    return "Announce start: " + " ".join(parts)
