from __future__ import annotations

import time


class TimeoutTimer:
    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = max(1, timeout_ms)
        self.started = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    @property
    def remaining_ms(self) -> int:
        return max(1, self.timeout_ms - self.elapsed_ms)

    @property
    def remaining_seconds(self) -> float:
        return self.remaining_ms / 1000
