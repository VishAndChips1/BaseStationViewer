"""Refresh-rate measurement and the one place that formats "N points @ X Hz".

A frame counter divided by a fixed window (the old approach) is wrong at both
ends: at 1 Hz it flickers between 0 and 1, and at 0.4 Hz it says 0 most of the
time. `RateMeter` instead measures the time between recent arrivals, so it is
steady at any rate, and it decays toward zero when frames stop arriving rather
than freezing on the last good number.
"""

import threading
import time
from collections import deque


class RateMeter:
    """Events per second, from the spacing of the most recent events. Thread-safe."""

    def __init__(self, max_events: int = 12, max_age: float = 10.0):
        self._lock = threading.Lock()
        self._times = deque(maxlen=max_events)
        self._max_age = max_age

    def tick(self, now: float | None = None) -> None:
        with self._lock:
            self._times.append(time.monotonic() if now is None else now)

    def reset(self) -> None:
        with self._lock:
            self._times.clear()

    def age(self, now: float | None = None) -> float | None:
        """Seconds since the last event, or None if there has been none."""
        now = time.monotonic() if now is None else now
        with self._lock:
            return None if not self._times else max(0.0, now - self._times[-1])

    def rate(self, now: float | None = None) -> float | None:
        """Events per second, or None until two events have been seen.

        Once the newest event is older than the usual gap, the true rate can
        only be lower than the last measurement, so it is capped at 1/age:
        the number falls smoothly instead of staying high until it drops to 0.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            seen_any = bool(self._times)
            times = [t for t in self._times if now - t <= self._max_age]
        if not times:
            return 0.0 if seen_any else None
        if len(times) < 2:
            return None if now - times[-1] < 1.0 else 0.0
        span = times[-1] - times[0]
        if span <= 0:
            return None
        rate = (len(times) - 1) / span
        gap = now - times[-1]
        if gap > 1.0 / rate:
            rate = min(rate, 1.0 / gap)
        return rate


def format_hz(hz: float | None) -> str:
    """"4.8 Hz", "12 Hz", "0.40 Hz"; "-" while unknown."""
    if hz is None or hz != hz:
        return "-"
    if hz <= 0.005:
        return "0 Hz"
    if hz < 1:
        return f"{hz:.2f} Hz"
    if hz < 10:
        return f"{hz:.1f} Hz"
    return f"{hz:.0f} Hz"


def format_cloud(points: int, hz: float | None, unit: str = "points") -> str:
    """"123,456 points @ 4.8 Hz" -- every place a cloud size is shown uses this,
    so the refresh rate always travels with the point count."""
    return f"{points:,} {unit} @ {format_hz(hz)}"
