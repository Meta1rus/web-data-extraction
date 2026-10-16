"""Предохранитель (circuit breaker) на уровне домена (п. 4.3).

closed    — запросы идут; считаем подряд идущие отказы домена (5xx, таймауты, сетевые ошибки);
open      — после `fail_threshold` отказов подряд запросы к домену НЕ отправляются `reset_after` секунд;
half-open — по истечении паузы пропускается ровно один пробный запрос:
            успех → closed, отказ → снова open (пауза удваивается до `max_reset`).

429 предохранитель не размыкает: это просьба «сбавь скорость», её обрабатывают
ограничитель (пауза домена) и политика повторов. 403/капча — зона детектора блокировок.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half-open"


@dataclass
class CircuitBreaker:
    host: str
    fail_threshold: int = 3
    reset_after: float = 10.0
    max_reset: float = 120.0
    clock: Callable[[], float] = time.monotonic
    on_transition: Callable[[str, str, str, str], None] | None = None   # (host, from, to, reason)
    state: str = CLOSED
    failures: int = 0
    opened_at: float = 0.0
    current_reset: float = field(default=0.0)
    probe_in_flight: bool = False
    activations: int = 0          # сколько раз переходил в open
    rejected: int = 0             # сколько запросов не отправлено, пока был open

    def __post_init__(self):
        self.current_reset = self.reset_after

    def _move(self, new: str, reason: str) -> None:
        old, self.state = self.state, new
        if new == OPEN:
            self.opened_at = self.clock()
            self.activations += 1
        if self.on_transition and old != new:
            self.on_transition(self.host, old, new, reason)

    def allow(self) -> bool:
        """Можно ли отправить запрос сейчас. В half-open пропускает один пробный запрос."""
        if self.state == OPEN:
            if self.clock() - self.opened_at >= self.current_reset:
                self._move(HALF_OPEN, f"пауза {self.current_reset:.0f} с истекла — пробный запрос")
            else:
                self.rejected += 1
                return False
        if self.state == HALF_OPEN:
            if self.probe_in_flight:
                self.rejected += 1
                return False
            self.probe_in_flight = True
        return True

    def retry_at(self) -> float:
        return self.opened_at + self.current_reset

    def record_success(self) -> None:
        if self.state == HALF_OPEN:
            self.probe_in_flight = False
            self.current_reset = self.reset_after
            self._move(CLOSED, "пробный запрос успешен")
        self.failures = 0

    def record_failure(self, reason: str = "") -> None:
        self.failures += 1
        if self.state == HALF_OPEN:
            self.probe_in_flight = False
            self.current_reset = min(self.max_reset, self.current_reset * 2)
            self._move(OPEN, f"пробный запрос неудачен ({reason}); пауза {self.current_reset:.0f} с")
        elif self.state == CLOSED and self.failures >= self.fail_threshold:
            self._move(OPEN, f"{self.failures} отказов подряд ({reason}); пауза {self.current_reset:.0f} с")


class BreakerRegistry:
    """Структура «домен → предохранитель»."""

    def __init__(self, fail_threshold: int = 3, reset_after: float = 10.0, max_reset: float = 120.0,
                 on_transition=None):
        self.params = {"fail_threshold": fail_threshold, "reset_after": reset_after, "max_reset": max_reset}
        self.on_transition = on_transition
        self.breakers: dict[str, CircuitBreaker] = {}

    def get(self, host: str) -> CircuitBreaker:
        if host not in self.breakers:
            self.breakers[host] = CircuitBreaker(host=host, on_transition=self.on_transition, **self.params)
        return self.breakers[host]

    def activations(self) -> int:
        return sum(b.activations for b in self.breakers.values())

    def summary(self) -> dict:
        return {h: {"state": b.state, "activations": b.activations, "rejected": b.rejected}
                for h, b in self.breakers.items()}
