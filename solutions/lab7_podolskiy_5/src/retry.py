"""Политика повторов (п. 4.2): классификация ошибок, full jitter, cap, лимит попыток,
бюджет времени на URL, приоритет Retry-After (число секунд и HTTP-дата).

Правила:
* повторяем только временные сбои: сетевые ошибки/таймауты, 408, 429, 500/502/503/504/522/524;
* 4xx (кроме 408 и 429) не повторяем никогда;
* 401 — «нужна (новая) авторизация»: повтор бессмыслен без обновления токена; наш сборщик
  работает без авторизации, поэтому фиксируем событие и прекращаем попытки;
* 403 — «доступ запрещён / возможная блокировка»: без повторов, инцидент передаётся детектору;
* Retry-After важнее собственного расчёта: ждём не меньше, чем просит сервер;
  если просьба сервера не укладывается в бюджет URL — откладываем URL, а не «повторяем раньше».
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 522, 524}
# Имена исключений Twisted/Scrapy/requests, означающие временный сетевой сбой
NETWORK_ERRORS = {
    "TimeoutError", "TCPTimedOutError", "ConnectionRefusedError", "ConnectionLost",
    "ConnectionDone", "ResponseNeverReceived", "ResponseFailed", "ConnectError",
    "ConnectionResetError", "ReadTimeout", "ConnectTimeout", "ConnectionError", "PartialDownloadError",
}
DNS_ERRORS = {"DNSLookupError", "gaierror"}


@dataclass(frozen=True)
class Decision:
    error_class: str          # класс ошибки для журнала и отчёта
    retry: bool               # повторять ли
    action: str               # что делать помимо повтора


def classify(status: int | None = None, exc: BaseException | str | None = None) -> Decision:
    """Код ответа / исключение → класс ошибки → повторять или нет."""
    if exc is not None:
        name = exc if isinstance(exc, str) else type(exc).__name__
        if name in DNS_ERRORS:
            return Decision("dns", True, "повтор с backoff; при устойчивом отказе — предохранитель")
        if "Timeout" in name or "TimedOut" in name:
            return Decision("network_timeout", True, "повтор с backoff; считается отказом домена")
        if name in NETWORK_ERRORS:
            return Decision("network_error", True, "повтор с backoff; считается отказом домена")
        return Decision("network_other", True, "повтор с backoff; считается отказом домена")
    if status is None:
        return Decision("unknown", False, "в журнал")
    if 200 <= status < 300:
        return Decision("success", False, "—")
    if status == 304:
        return Decision("not_modified", False, "ресурс не изменился: берём сохранённую версию")
    if 300 <= status < 400:
        return Decision("redirect", False, "следуем редиректу (RedirectMiddleware)")
    if status == 401:
        return Decision("auth_required", False, "обновить токен; без авторизации — прекратить попытки")
    if status == 403:
        return Decision("forbidden_possible_block", False, "без повторов; инцидент → детектор блокировок")
    if status == 408:
        return Decision("request_timeout", True, "повтор с backoff")
    if status == 429:
        return Decision("rate_limited", True, "снизить скорость домена; ждать Retry-After или backoff")
    if status in (404, 410):
        return Decision("not_found", False, "в журнал; URL исключить")
    if 400 <= status < 500:
        return Decision("client_error", False, "в журнал; ошибка запроса")
    if status in RETRYABLE_STATUS:
        return Decision("server_error", True, "повтор с backoff; считается отказом домена")
    if status >= 500:
        return Decision("server_error_permanent", False, "в журнал")
    return Decision("other", False, "в журнал")


def classification_table() -> list[dict]:
    """Таблица классов для отчёта (код/ситуация → класс → повторять → действие)."""
    rows = []
    for code in (200, 304, 301, 400, 401, 403, 404, 408, 422, 429, 500, 501, 502, 503, 504):
        d = classify(code)
        rows.append({"код/ситуация": code, "класс": d.error_class, "повторять": "да" if d.retry else "нет",
                     "действие": d.action})
    for exc in ("TimeoutError", "ConnectionRefusedError", "DNSLookupError"):
        d = classify(exc=exc)
        rows.append({"код/ситуация": exc, "класс": d.error_class, "повторять": "да" if d.retry else "нет",
                     "действие": d.action})
    return rows


def parse_retry_after(value: str | bytes | None, now: float | None = None) -> float | None:
    """Retry-After: «120» (секунды) или «Wed, 21 Oct 2026 07:28:00 GMT» (HTTP-дата) → секунды ≥ 0."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("latin-1", errors="replace")
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(int(value))
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    now = time.time() if now is None else now
    return max(0.0, dt.timestamp() - now)


def full_jitter(attempt: int, base: float = 0.5, cap: float = 20.0, rnd=random.uniform) -> float:
    """AWS «Exponential Backoff and Jitter»: sleep = random(0, min(cap, base·2^attempt))."""
    return rnd(0.0, min(cap, base * (2 ** attempt)))


@dataclass
class RetryPolicy:
    max_attempts: int = 4            # всего попыток, включая первую
    base: float = 0.5                # с: база экспоненты
    cap: float = 20.0                # с: потолок одной паузы backoff
    budget_s: float = 90.0           # с: общий бюджет времени на один URL (от первой попытки)
    retry_after_cap: float = 120.0   # с: дольше этого ждать не будем — откладываем URL

    def next_delay(self, attempt: int, status: int | None, retry_after: str | bytes | None,
                   elapsed_s: float, exc: BaseException | str | None = None) -> tuple[float | None, str, str]:
        """attempt — номер завершившейся попытки (1, 2, ...).

        Возвращает (задержка | None, источник задержки, причина): None — повторов больше нет.
        """
        d = classify(status, exc)
        if not d.retry:
            return None, "none", f"не повторяем: {d.error_class}"
        if attempt >= self.max_attempts:
            return None, "none", f"исчерпаны попытки ({self.max_attempts})"
        ra = parse_retry_after(retry_after) if status in (429, 503) else None
        if ra is not None:
            if ra > self.retry_after_cap or elapsed_s + ra > self.budget_s:
                return None, "retry-after", f"Retry-After={ra:.0f} с не укладывается в бюджет — URL отложен"
            return ra, "retry-after", d.error_class
        delay = full_jitter(attempt, self.base, self.cap)
        if elapsed_s + delay > self.budget_s:
            return None, "backoff", f"бюджет {self.budget_s:.0f} с на URL исчерпан"
        return delay, "backoff", d.error_class
