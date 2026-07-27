from __future__ import annotations

import time


class PainPointDebouncer:
    def __init__(self, cooldown_seconds: int = 45) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._last_trigger: dict[str, float] = {}

    def should_trigger(self, category: str) -> bool:
        now = time.monotonic()
        last = self._last_trigger.get(category)
        if last is None:
            return True
        return (now - last) >= self.cooldown_seconds

    def record_trigger(self, category: str) -> None:
        self._last_trigger[category] = time.monotonic()
