"""Управление скоростью (п. 4.1): token bucket по доменам, джиттер, адаптивная задержка,
чтение robots.txt и общий межпроцессный счётчик для нескольких воркеров.

Модуль не зависит от Scrapy: те же классы используются в Scrapy-middleware
(src/crawler/middlewares.py) и в демонстрационных скриптах.

Неблокирующая схема: limiter не спит сам, а возвращает, сколько секунд подождать
(reserve). Scrapy-middleware ждёт это время асинхронно, не останавливая цикл событий;
демонстрационный скрипт с процессами — обычным sleep.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlsplit

try:                                    # Protego ставится вместе со Scrapy; понимает * и $ (RFC 9309)
    from protego import Protego
except ImportError:                     # без него остаётся только urllib.robotparser
    Protego = None

try:
    import fcntl                        # POSIX (macOS, Linux): блокировка файла для общего счётчика
except ImportError:
    fcntl = None


# --------------------------------------------------------------------------- token bucket
class TokenBucket:
    """Ведро токенов: `rate` запросов в секунду, всплеск до `burst` запросов.

    reserve() забирает токен «в долг» и возвращает время ожидания: так несколько
    одновременных запросов выстраиваются в очередь с правильными интервалами.
    """

    def __init__(self, rate: float, burst: int = 1, clock=time.monotonic):
        if rate <= 0:
            raise ValueError("rate должен быть > 0")
        self.rate, self.burst, self.clock = float(rate), int(burst), clock
        self.tokens = float(burst)
        self.updated = clock()

    def _refill(self, now: float) -> None:
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def reserve(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        self._refill(now)
        self.tokens -= 1
        return 0.0 if self.tokens >= 0 else -self.tokens / self.rate

    def peek(self, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        return 0.0 if tokens >= 1 else (1 - tokens) / self.rate


# --------------------------------------------------------------------------- адаптивная задержка
class AdaptiveDelay:
    """Задержка между запросами к домену по измеренной латентности сервера.

    target = латентность / целевая_параллельность (логика AutoThrottle).
    Сервер замедлился → задержка сразу поднимается до target; ускорился → снижается
    плавно (75 % старого + 25 % нового). Ответ 429/503 удваивает задержку (не меньше 1 с).
    """

    def __init__(self, target_concurrency: float = 1.0, start: float = 0.5,
                 min_delay: float = 0.2, max_delay: float = 10.0):
        self.target_concurrency = target_concurrency
        self.delay, self.min_delay, self.max_delay = start, min_delay, max_delay
        self.penalties = 0

    def observe(self, latency_s: float, status: int | None) -> float:
        if status in (429, 503):
            self.delay = max(self.delay * 2, 1.0)
            self.penalties += 1
        else:
            target = latency_s / self.target_concurrency
            self.delay = target if target > self.delay else 0.75 * self.delay + 0.25 * target
        self.delay = min(self.max_delay, max(self.min_delay, self.delay))
        return self.delay


# --------------------------------------------------------------------------- политика домена
@dataclass
class DomainPolicy:
    """Все ограничения скорости одного домена: bucket + адаптивная задержка + Crawl-delay + пауза."""
    host: str
    rate: float
    burst: int = 1
    jitter: float = 0.25                 # 20–30 % — по заданию
    crawl_delay: float | None = None     # из robots.txt: имеет приоритет над нашими настройками
    adaptive: AdaptiveDelay | None = None
    pause_until: float = 0.0             # пауза домена после 429/503 (Retry-After)
    last_start: float = float("-inf")
    bucket: TokenBucket = field(init=False)

    def __post_init__(self):
        self.bucket = TokenBucket(self.rate, self.burst)

    def base_interval(self) -> float:
        return max(1.0 / self.rate,
                   self.adaptive.delay if self.adaptive else 0.0,
                   self.crawl_delay or 0.0)

    def reserve(self, now: float | None = None) -> tuple[float, str]:
        """Возвращает (секунд ждать, что ограничило). Порядок важен: резервирование в очереди."""
        now = time.monotonic() if now is None else now
        candidates = {"token_bucket": now + self.bucket.reserve(now)}
        if self.adaptive:
            candidates["adaptive_delay"] = self.last_start + self.adaptive.delay
        if self.crawl_delay:
            candidates["crawl_delay"] = self.last_start + self.crawl_delay
        candidates["pause_429_503"] = self.pause_until
        reason, start = max(candidates.items(), key=lambda kv: kv[1])
        start = max(start, now)
        if self.jitter:
            start += random.uniform(0, self.jitter * self.base_interval())
        self.last_start = start
        return start - now, (reason if start - now > 0 else "none")

    def peek(self, now: float | None = None) -> float:
        """Сколько ждать следующему запросу (без резервирования токена)."""
        now = time.monotonic() if now is None else now
        cands = [now + self.bucket.peek(now), self.pause_until]
        if self.adaptive:
            cands.append(self.last_start + self.adaptive.delay)
        if self.crawl_delay:
            cands.append(self.last_start + self.crawl_delay)
        return max(0.0, max(cands) - now)

    def penalize(self, seconds: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.pause_until = max(self.pause_until, now + seconds)


class DomainLimiter:
    """Структура «домен → ограничитель»: лимиты задаются по домену, а не глобально."""

    def __init__(self, default_rate: float = 1.0, default_burst: int = 2, jitter: float = 0.25,
                 adaptive: bool = True, per_domain: dict[str, dict] | None = None):
        self.default_rate, self.default_burst, self.jitter = default_rate, default_burst, jitter
        self.adaptive = adaptive
        self.per_domain = per_domain or {}
        self.domains: dict[str, DomainPolicy] = {}

    def policy(self, host: str) -> DomainPolicy:
        if host not in self.domains:
            cfg = self.per_domain.get(host, {})
            self.domains[host] = DomainPolicy(
                host=host, rate=cfg.get("rate", self.default_rate), burst=cfg.get("burst", self.default_burst),
                jitter=cfg.get("jitter", self.jitter), crawl_delay=cfg.get("crawl_delay"),
                adaptive=AdaptiveDelay(start=cfg.get("start_delay", 0.5)) if self.adaptive else None)
        return self.domains[host]

    def reserve(self, host: str) -> tuple[float, str]:
        return self.policy(host).reserve()

    def peek(self, host: str) -> float:
        return self.policy(host).peek()

    def observe(self, host: str, latency_s: float, status: int | None) -> float | None:
        pol = self.policy(host)
        return pol.adaptive.observe(latency_s, status) if pol.adaptive else None

    def penalize(self, host: str, seconds: float) -> None:
        self.policy(host).penalize(seconds)


# --------------------------------------------------------------------------- общий счётчик (4 процесса)
class SharedTokenBucket:
    """Межпроцессный token bucket: состояние в JSON-файле, доступ под fcntl.flock.

    Все воркеры, запущенные на одной машине, берут токены из одного ведра, поэтому
    суммарная скорость на домен не превышает rate независимо от числа процессов.
    Время — time.time() (общие часы для всех процессов).
    """

    def __init__(self, path: str | Path, rate: float, burst: int = 1):
        if fcntl is None:
            raise RuntimeError("SharedTokenBucket требует POSIX (macOS/Linux)")
        self.path, self.rate, self.burst = Path(path), float(rate), int(burst)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def reserve(self) -> float:
        with open(self.path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                state = json.loads(raw) if raw else {"tokens": self.burst, "updated": time.time()}
                now = time.time()
                tokens = min(self.burst, state["tokens"] + (now - state["updated"]) * self.rate) - 1
                f.seek(0)
                f.truncate()
                f.write(json.dumps({"tokens": tokens, "updated": now}))
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return 0.0 if tokens >= 0 else -tokens / self.rate


# --------------------------------------------------------------------------- robots.txt
@dataclass
class RobotsReport:
    origin: str
    robots_url: str
    status: int | None
    found: bool
    lines: int
    crawl_delay: float | None
    request_rate: tuple[int, int] | None
    sitemaps: list[str]
    can_fetch: dict[str, bool]
    can_fetch_urllib: dict[str, bool]
    can_fetch_protego: dict[str, bool | None]
    policy_note: str
    text: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def effective_rate(self, own_rate: float) -> float:
        """Request-rate из robots.txt ограничивает нашу скорость сверху."""
        if self.request_rate:
            req, sec = self.request_rate
            return min(own_rate, req / sec)
        return own_rate


def read_robots(origin: str, user_agent: str, probe_urls: list[str], timeout: float = 15.0) -> RobotsReport:
    """Читает robots.txt через urllib.robotparser (требование задания) и перепроверяет Protego.

    Итоговый can_fetch — строже из двух ответов: urllib.robotparser в Python ≤ 3.12 не понимает
    шаблоны (*, $) и на «Disallow: /ecommerce/*» ошибочно разрешает обход.
    Трактовка кодов (RFC 9309, п. 2.3.1): 2xx — правила из файла; 4xx — файла нет, явных
    ограничений нет (но условия использования сайта действуют!); 5xx и сетевая ошибка —
    считаем, что запрещено всё. Для 401/403 urllib.robotparser.read() запрещает всё — мы
    поступаем так же (строже, чем RFC).
    """
    robots_url = origin.rstrip("/") + "/robots.txt"
    req = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
    status, text = None, ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            text = resp.read(512 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        text = f"# network error: {e}"

    rp = robotparser.RobotFileParser(robots_url)
    pg = None
    if status is not None and 200 <= status < 300:
        rp.parse(text.splitlines())
        pg = Protego.parse(text) if Protego else None
        note = "robots.txt найден: применяются его правила; Crawl-delay имеет приоритет над нашими лимитами"
    elif status in (401, 403):
        rp.disallow_all = True
        note = f"HTTP {status}: доступ к robots.txt запрещён — считаем, что запрещено всё (как urllib.robotparser)"
    elif status is not None and 400 <= status < 500:
        rp.allow_all = True
        note = (f"HTTP {status}: файла нет — явных запретов нет, но это не разрешение на любую нагрузку: "
                "действуют условия использования сайта и собственные лимиты")
    else:
        rp.disallow_all = True
        note = "robots.txt недоступен (5xx/сеть) — по RFC 9309 обход не начинаем"

    urllib_ok = {u: rp.can_fetch(user_agent, u) for u in probe_urls}
    protego_ok = {u: (pg.can_fetch(u, user_agent) if pg else None) for u in probe_urls}
    final = {u: urllib_ok[u] and (protego_ok[u] is not False) for u in probe_urls}
    rr = rp.request_rate(user_agent)
    crawl_delay = rp.crawl_delay(user_agent)
    if crawl_delay is None and pg is not None:
        crawl_delay = pg.crawl_delay(user_agent)
    return RobotsReport(
        origin=origin, robots_url=robots_url, status=status,
        found=status is not None and 200 <= status < 300,
        lines=len(text.splitlines()) if text and status and status < 300 else 0,
        crawl_delay=float(crawl_delay) if crawl_delay is not None else None,
        request_rate=(rr.requests, rr.seconds) if rr else None,
        sitemaps=list(rp.site_maps() or []),
        can_fetch=final, can_fetch_urllib=urllib_ok, can_fetch_protego=protego_ok,
        policy_note=note, text=text if status and status < 300 else "",
    )


def origin_of(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"
