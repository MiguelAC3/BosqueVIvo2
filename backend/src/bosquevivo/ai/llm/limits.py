import threading
import time
from collections import deque
from collections.abc import Callable

from bosquevivo.ai.llm.errors import QuotaExceededError


class RateLimiter:
    def __init__(
        self,
        per_minute: int,
        per_day: int,
        clock: Callable[[], float] = time.monotonic,
        max_wait: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.per_minute = per_minute
        self.per_day = per_day
        self.clock = clock
        self.max_wait = max_wait
        self.sleep = sleep
        self._minute: deque[float] = deque()
        self._day: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Si el minuto está lleno espera hasta `max_wait` segundos a que se libere un cupo.
        El límite diario no espera."""
        deadline = self.clock() + self.max_wait
        while True:
            try:
                return self._try_acquire()
            except QuotaExceededError:
                with self._lock:
                    day_full = len(self._day) >= self.per_day
                    free_at = self._minute[0] + 60 if self._minute else self.clock()
                if day_full or free_at > deadline:
                    raise
                self.sleep(max(free_at - self.clock(), 0.05))

    def _try_acquire(self) -> None:
        with self._lock:
            now = self.clock()
            while self._minute and now - self._minute[0] > 60:
                self._minute.popleft()
            while self._day and now - self._day[0] > 86_400:
                self._day.popleft()
            if len(self._minute) >= self.per_minute or len(self._day) >= self.per_day:
                raise QuotaExceededError("se alcanzó el límite de peticiones al modelo")
            self._minute.append(now)
            self._day.append(now)

    @property
    def used_today(self) -> int:
        return len(self._day)
