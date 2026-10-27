"""Client-side pacing so the agents slow down before HubSpot returns a 429, not after."""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    """Paces requests to stay inside HubSpot's rolling window (the TEN_SECONDLY_ROLLING policy).

    A token bucket lets through its capacity plus whatever refills during a window, so both are sized
    from the allowance (the limit less a headroom margin): a quarter of it as burst, the rest spread over
    the window. The limit itself comes from the X-HubSpot-RateLimit-Max and -Interval-Milliseconds headers,
    because it depends on the portal's tier. The bucket never holds more tokens than -Remaining allows,
    since the worker and the nightly runs share one app's limit, and when -Remaining is down to the margin
    it waits out a full window before the next request.
    """

    def __init__(self, max_requests: int = 100, interval_s: float = 10.0, headroom: float = 0.9, burst: float = 0.25,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.headroom, self.burst = headroom, burst
        self.clock, self.sleep = clock, sleep
        self._configure(max_requests, interval_s)
        self.tokens = self.capacity
        self.updated = clock()
        self.low = False
        self.waits = 0

    def _configure(self, max_requests: int, interval_s: float) -> None:
        self.max_requests, self.interval_s = max_requests, interval_s
        allowance = max_requests * self.headroom
        self.capacity = max(1.0, allowance * self.burst)
        self.rate = max(allowance - self.capacity, 1.0) / interval_s
        self.margin = max_requests - allowance

    def acquire(self) -> None:
        if self.low:
            self.waits += 1
            self.sleep(self.interval_s)
            self.low, self.tokens, self.updated = False, self.capacity, self.clock()
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            self.waits += 1
            self.sleep((1 - self.tokens) / self.rate)
            self.tokens, self.updated = 1.0, self.clock()
        self.tokens -= 1

    def observe(self, headers) -> None:
        try:
            max_requests = int(headers.get("X-HubSpot-RateLimit-Max", 0))
            interval_ms = int(headers.get("X-HubSpot-RateLimit-Interval-Milliseconds", 0))
            remaining = headers.get("X-HubSpot-RateLimit-Remaining")
        except (TypeError, ValueError):
            return
        if max_requests and interval_ms and (max_requests, interval_ms / 1000) != (self.max_requests, self.interval_s):
            self._configure(max_requests, interval_ms / 1000)
            self.tokens = min(self.tokens, self.capacity)
        if remaining is not None and max_requests:
            self.tokens = min(self.tokens, max(0.0, int(remaining) - self.margin))
            self.low = int(remaining) <= self.margin
