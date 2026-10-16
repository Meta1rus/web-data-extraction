"""Вежливый и устойчивый HTTP-клиент — единая точка выхода в сеть для обоих источников.

Что здесь реализовано (шаблоны демо-блокнотов занятий 3 и 7, адаптированные под GitHub):
  * ограничитель скорости token bucket ПО ДОМЕНУ (ключу лимита) с джиттером; Crawl-delay имеет приоритет;
  * классификация ошибок и повторы только временных сбоев; Retry-After / X-RateLimit-Reset важнее backoff;
  * экспоненциальная задержка с full jitter; снижение темпа домена после 429/503 («сбавь скорость»);
  * предохранитель (circuit breaker) closed -> open -> half-open на каждый домен;
  * условные запросы If-None-Match / If-Modified-Since (state/etags.json) -> 304;
  * robots.txt: urllib.robotparser + собственный разбор шаблонов * и $ по RFC 9309
    (стандартный robotparser шаблоны не понимает — см. tests/test_parser.py::test_robots_*);
  * кэш HTML на диске только для отладки (флаг --debug-cache);
  * структурная запись КАЖДОЙ попытки в журнал прогона.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib import robotparser
from urllib.parse import urlparse, urlunparse

import requests

from observability import RunContext
from settings import CACHE_DIR, DIRS, is_local_url, iso, read_json, utcnow, write_json_atomic


class SimulatedCrash(RuntimeError):
    """Искусственный обрыв прогона для демонстрации возобновляемости (--simulate-crash-after N)."""


class RequestBudgetExceeded(RuntimeError):
    """Сработал предохранитель max_requests_per_run."""


# ------------------------------------------------------------------ ограничение скорости

class RateLimiter:
    """Token bucket: rate запросов в секунду, всплеск до burst; джиттер — доля интервала 1/rate."""

    def __init__(self, rate: float, burst: int = 1, jitter: float = 0.3, min_rate: float | None = None):
        if rate <= 0:
            raise ValueError("rate должен быть > 0")
        self.rate = rate
        self.base_rate = rate
        self.min_rate = min_rate or rate / 8
        self.burst = max(1, int(burst))
        self.jitter = jitter
        self.tokens = float(self.burst)
        self.updated = time.monotonic()
        self.waited_s = 0.0

    def acquire(self, max_wait_s: float = 600.0) -> float:
        """Блокирует до разрешения запроса. Возвращает фактическое ожидание, с."""
        started = time.monotonic()
        while time.monotonic() - started <= max_wait_s:   # предохранитель: ожидание ограничено
            now = time.monotonic()
            self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                if self.jitter:
                    time.sleep(random.uniform(0, self.jitter / self.rate))
                waited = time.monotonic() - started
                self.waited_s += waited
                return waited
            time.sleep(min((1 - self.tokens) / self.rate, 5.0))
        raise TimeoutError("ограничитель скорости ждал дольше max_wait_s")

    def penalize(self) -> float:
        """Ответ 429/503: вдвое снижаем темп домена (не ниже min_rate) — реакция «сбавить скорость»."""
        self.rate = max(self.min_rate, self.rate / 2)
        return self.rate


# ------------------------------------------------------------------ классификация ошибок

def classify(status: int | None, exc: Exception | None = None,
             headers: dict | None = None, body_snippet: str = "") -> tuple[str, bool]:
    """(класс_ошибки, повторять_ли). 4xx, кроме 429 и лимитов GitHub, не повторяем."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    if exc is not None:
        if isinstance(exc, requests.Timeout):
            return "timeout", True
        if isinstance(exc, requests.ConnectionError):
            return "connection_error", True
        return "network_error", True
    if status is None:
        return "network_error", True
    if 200 <= status < 300:
        return "ok", False
    if status == 304:
        return "not_modified", False
    if status == 429:
        return "rate_limited", True
    if status == 403:
        # GitHub: исчерпанная основная квота -> 403 и X-RateLimit-Remaining: 0;
        # вторичный лимит -> 403/429 с Retry-After или текстом «secondary rate limit».
        if headers.get("x-ratelimit-remaining") == "0":
            return "rate_limit_primary", True
        if "retry-after" in headers or "secondary rate limit" in body_snippet.lower():
            return "rate_limit_secondary", True
        return "forbidden", False
    if status in (401,):
        return "unauthorized", False
    if status == 404:
        return "not_found", False
    if status in (500, 502, 503, 504):
        return "server_error", True
    if 400 <= status < 500:
        return "client_error", False
    return "other", False


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Экспоненциальная задержка с full jitter (AWS Architecture Blog): U(0, min(cap, base*2^attempt))."""
    return random.uniform(0, min(cap, base * 2 ** attempt))


def server_requested_wait(headers: dict, cls: str) -> tuple[float | None, str]:
    """Сколько просит подождать сервер: Retry-After (секунды или HTTP-дата) или X-RateLimit-Reset."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    ra = h.get("retry-after")
    if ra:
        ra = ra.strip()
        if ra.isdigit():
            return float(ra), "Retry-After"
        try:
            dt = parsedate_to_datetime(ra)
            return max(0.0, (dt - utcnow()).total_seconds()), "Retry-After"
        except (TypeError, ValueError):
            ra = None  # формат Retry-After не распознан — переходим к лимитам GitHub / backoff
    if cls == "rate_limit_primary" and h.get("x-ratelimit-reset", "").isdigit():
        return max(0.0, int(h["x-ratelimit-reset"]) - time.time()) + 1.0, "X-RateLimit-Reset"
    if cls == "rate_limit_secondary":
        return 60.0, "GitHub docs: вторичный лимит — ждать не менее минуты"
    return None, "backoff"


# ------------------------------------------------------------------ предохранитель

class CircuitBreaker:
    """Предохранитель на один домен: closed -> open (пауза) -> half-open (одна проба) -> closed."""

    def __init__(self, fail_threshold: int = 5, reset_after_s: float = 60.0):
        self.fail_threshold = fail_threshold
        self.reset_after_s = reset_after_s
        self.state = "closed"
        self.failures = 0
        self.opened_at = 0.0
        self.activations = 0

    def allow(self) -> bool:
        if self.state == "open":
            if time.monotonic() - self.opened_at >= self.reset_after_s:
                self.state = "half-open"
                return True
            return False
        return True

    def record(self, ok: bool) -> str:
        if ok:
            self.state, self.failures = "closed", 0
        else:
            self.failures += 1
            if self.state == "half-open" or self.failures >= self.fail_threshold:
                self.trip()
        return self.state

    def trip(self) -> None:
        if self.state != "open":
            self.activations += 1
        self.state, self.opened_at = "open", time.monotonic()


# ------------------------------------------------------------------ условные запросы

class EtagStore:
    """state/etags.json: {url: {etag, last_modified, saved_at, status}}."""

    def __init__(self, path=None):
        self.path = path or DIRS["state"] / "etags.json"
        self.data: dict = read_json(self.path, {})
        self.dirty = 0

    # Хосты, где условный запрос делается только по Last-Modified (config.yaml: http.conditional_ims_only_hosts).
    # Причина (реальный прогон 15.10.2026): ETag ответа api.github.com/repos/{o}/{r} менялся между запросами
    # при неизменном Last-Modified, а по RFC 9110 §13.1.3 при наличии If-None-Match сервер игнорирует
    # If-Modified-Since — поэтому вместо 304 приходил 200. Запрос только с If-Modified-Since даёт 304.
    ims_only_hosts: set = set()

    def headers_for(self, url: str) -> dict:
        rec = self.data.get(url) or {}
        h = {}
        if urlparse(url).hostname in self.ims_only_hosts and rec.get("last_modified"):
            return {"If-Modified-Since": rec["last_modified"]}
        if rec.get("etag"):
            h["If-None-Match"] = rec["etag"]
        if rec.get("last_modified"):
            h["If-Modified-Since"] = rec["last_modified"]
        return h

    def update(self, url: str, resp: requests.Response) -> None:
        etag, lm = resp.headers.get("ETag"), resp.headers.get("Last-Modified")
        if etag or lm:
            self.data[url] = {"etag": etag, "last_modified": lm, "saved_at": iso(utcnow())}
            self.dirty += 1

    def save(self) -> None:
        if self.dirty:
            write_json_atomic(self.path, self.data)
            self.dirty = 0


# ------------------------------------------------------------------ robots.txt

def _rfc9309_pattern(path: str) -> re.Pattern:
    """Шаблон пути robots.txt -> regex: * — любая последовательность, $ в конце — конец URL."""
    anchored = path.endswith("$")
    body = path[:-1] if anchored else path
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + rx + ("$" if anchored else ""))


class RobotsRules:
    """Правила одной группы robots.txt с матчингом по RFC 9309 (самое длинное совпадение, Allow при равенстве)."""

    def __init__(self, text: str, user_agent: str):
        self.text = text
        self.user_agent = user_agent
        self.group_agent, self.rules, self.crawl_delay = self._select_group(text, user_agent)
        self.sitemaps = [ln.split(":", 1)[1].strip() for ln in text.splitlines()
                         if ln.lower().startswith("sitemap:")]

    @staticmethod
    def _select_group(text: str, user_agent: str):
        product = user_agent.split("/")[0].strip().lower()
        groups: list[tuple[list[str], list[tuple[str, str]], float | None]] = []
        agents: list[str] = []
        rules: list[tuple[str, str]] = []
        delay = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (p.strip() for p in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules, delay))
                    agents, rules, delay = [], [], None
                agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if key in ("allow", "disallow"):
                if value:
                    rules.append((key, value))
            elif key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    delay = None
        if agents or rules:
            groups.append((agents, rules, delay))
        for ags, rls, d in groups:                       # точное совпадение имени продукта
            if product in ags:
                return product, rls, d
        for ags, rls, d in groups:                       # иначе группа *
            if "*" in ags:
                return "*", rls, d
        return None, [], None

    def can_fetch(self, url: str) -> tuple[bool, str | None]:
        p = urlparse(url)
        target = (p.path or "/") + (f"?{p.query}" if p.query else "")
        best: tuple[int, str, str] | None = None
        for kind, path in self.rules:
            if _rfc9309_pattern(path).match(target):
                cand = (len(path), kind, path)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and kind == "allow"):
                    best = cand
        if best is None:
            return True, None
        return best[1] == "allow", f"{best[1]}: {best[2]}"


@dataclass
class RobotsReport:
    robots_url: str
    status: int | None
    available: bool
    group: str | None
    crawl_delay: float | None
    request_rate: str | None
    sitemaps: list
    checks: list                # [{url, rfc9309, rule, urllib_robotparser}]
    saved_copy: str | None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


# ------------------------------------------------------------------ результат запроса

@dataclass
class FetchResult:
    url: str
    status: int | None = None
    response: requests.Response | None = None
    error_class: str = ""
    attempts: int = 0
    from_cache: bool = False
    skipped: str | None = None          # robots | breaker_open | budget | wait_too_long

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def _strip_auth(url: str) -> str:
    p = urlparse(url)
    return urlunparse(p._replace(netloc=p.hostname + (f":{p.port}" if p.port else ""))) if p.username else url


class PoliteClient:
    """Единая точка выхода в сеть. Весь сетевой код проекта идёт через get()."""

    def __init__(self, cfg: dict, ctx: RunContext, etags: EtagStore | None = None,
                 rate_scale: float = 1.0, simulate_crash_after: int | None = None,
                 debug_cache: bool = False):
        self.cfg = cfg
        self.http = cfg["http"]
        self.ctx = ctx
        self.etags = etags or EtagStore()
        self.etags.ims_only_hosts = set(self.http.get("conditional_ims_only_hosts") or [])
        self.rate_scale = rate_scale
        self.simulate_crash_after = simulate_crash_after
        self.debug_cache = debug_cache
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.http["user_agent"],
                                     "Accept-Encoding": "gzip, deflate"})
        self.timeout = (self.http["connect_timeout_s"], self.http["read_timeout_s"])
        self.limiters: dict[str, RateLimiter] = {}
        self.breakers: dict[str, CircuitBreaker] = {}
        self.robots: dict[str, RobotsRules] = {}
        self.network_requests = 0

    # -- настройка доменов
    def register_limit(self, key: str, base_url: str, rate: float, burst: int,
                       crawl_delay: float | None = None) -> RateLimiter:
        """Лимит на домен/ключ. Crawl-delay из robots.txt имеет приоритет над нашей скоростью."""
        eff = rate
        if crawl_delay:
            eff = min(eff, 1.0 / crawl_delay)
        if self.rate_scale != 1.0:
            if is_local_url(base_url):
                eff = eff * min(self.rate_scale, float(self.http["local_rate_scale_max"]))
            else:   # к реальным сайтам ускорение не применяется никогда
                self.ctx.log("rate_scale_ignored", level="WARNING", key=key, base_url=base_url,
                             reason="--rate-scale разрешён только для локальной имитации 127.0.0.1")
        lim = RateLimiter(eff, burst, self.http["jitter"])
        self.limiters[key] = lim
        self.ctx.log("rate_limit_configured", key=key, rate_per_sec=round(eff, 3), burst=burst,
                     crawl_delay=crawl_delay, jitter=self.http["jitter"])
        return lim

    def breaker(self, host: str) -> CircuitBreaker:
        if host not in self.breakers:
            self.breakers[host] = CircuitBreaker(self.http["breaker_fail_threshold"],
                                                 self.http["breaker_reset_after_s"])
        return self.breakers[host]

    # -- robots.txt
    def check_robots(self, base_url: str, probe_urls: list[str]) -> RobotsReport:
        robots_url = base_url.rstrip("/") + "/robots.txt"
        res = self.get(robots_url, source="robots", limiter_key=None, conditional=False, robots_check=False)
        text = res.response.text if (res.ok and res.response is not None) else ""
        saved = None
        if text:
            path = DIRS["raw"] / "robots" / f"{urlparse(base_url).netloc.replace(':', '_')}_{utcnow():%Y%m%d}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            saved = f"raw/robots/{path.name}"
        rules = RobotsRules(text, self.http["user_agent"])
        self.robots[urlparse(base_url).netloc] = rules
        rp = robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        ua = self.http["user_agent"]
        checks = []
        for u in probe_urls:
            allowed, rule = rules.can_fetch(u)
            checks.append({"url": u, "rfc9309": allowed, "rule": rule, "urllib_robotparser": rp.can_fetch(ua, u)})
        rr = rp.request_rate(ua)
        report = RobotsReport(robots_url=robots_url, status=res.status, available=bool(text),
                              group=rules.group_agent, crawl_delay=rules.crawl_delay or rp.crawl_delay(ua),
                              request_rate=f"{rr.requests}/{rr.seconds}s" if rr else None,
                              sitemaps=rules.sitemaps, checks=checks, saved_copy=saved)
        self.ctx.log("robots_checked", **report.as_dict())
        return report

    # -- кэш для отладки
    @staticmethod
    def _cache_path(url: str):
        return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json.gz")

    def _cache_get(self, url: str) -> dict | None:
        p = self._cache_path(url)
        if not p.exists():
            return None
        age_h = (time.time() - p.stat().st_mtime) / 3600
        if age_h > self.http["debug_cache_ttl_hours"]:
            return None
        return json.loads(gzip.decompress(p.read_bytes()).decode("utf-8"))

    def _cache_put(self, url: str, resp: requests.Response) -> None:
        p = self._cache_path(url)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(gzip.compress(json.dumps({"status": resp.status_code, "text": resp.text,
                                                "headers": dict(resp.headers)}).encode("utf-8")))

    # -- основной метод
    def get(self, url: str, *, source: str, limiter_key: str | None, conditional: bool = True,
            headers: dict | None = None, robots_check: bool = True,
            cacheable: bool = False) -> FetchResult:
        host = urlparse(url).netloc
        log_url = _strip_auth(url)
        result = FetchResult(url=url)

        if robots_check and host in self.robots:
            allowed, rule = self.robots[host].can_fetch(url)
            if not allowed:
                self.ctx.log("request_skipped", level="WARNING", source=source, url=log_url,
                             reason="robots", rule=rule)
                result.skipped = "robots"
                return result

        if cacheable and self.debug_cache:
            cached = self._cache_get(url)
            if cached is not None:
                self.ctx.log("cache_hit", source=source, url=log_url, status=cached["status"])
                fake = requests.Response()
                fake.status_code = cached["status"]
                fake._content = cached["text"].encode("utf-8")
                fake.headers.update(cached["headers"])
                fake.encoding = "utf-8"
                fake.url = url
                result.status, result.response, result.from_cache = cached["status"], fake, True
                return result

        br = self.breaker(host)
        max_attempts = int(self.http["max_attempts"])
        for attempt in range(1, max_attempts + 1):
            result.attempts = attempt
            if not br.allow():
                self.ctx.log("request_skipped", level="WARNING", source=source, url=log_url,
                             reason="breaker_open", host=host)
                result.skipped = "breaker_open"
                return result
            if self.network_requests >= int(self.http["max_requests_per_run"]):
                raise RequestBudgetExceeded(f"достигнут лимит {self.http['max_requests_per_run']} запросов за прогон")
            if self.simulate_crash_after is not None and self.network_requests >= self.simulate_crash_after:
                raise SimulatedCrash(f"имитация обрыва после {self.network_requests} запросов")
            if limiter_key and limiter_key in self.limiters:
                self.limiters[limiter_key].acquire()

            req_headers = dict(headers or {})
            cond_headers = self.etags.headers_for(url) if conditional else {}
            req_headers.update(cond_headers)
            self.network_requests += 1
            t0 = time.perf_counter()
            resp, exc = None, None
            try:
                resp = self.session.get(url, headers=req_headers, timeout=self.timeout, allow_redirects=True)
                body = resp.content                        # тело читается здесь; raw.tell() — байты по сети
                wire = resp.raw.tell() if resp.raw is not None else None
                if not wire:  # chunked: urllib3 не считает байты по сети -> Content-Length или «неизвестно»
                    cl = resp.headers.get("Content-Length", "")
                    wire = int(cl) if cl.isdigit() else None
            except requests.RequestException as e:
                exc, body, wire = e, b"", None
            ms = round((time.perf_counter() - t0) * 1000, 1)
            status = resp.status_code if resp is not None else None
            snippet = body[:300].decode("utf-8", "replace") if (status and status >= 400) else ""
            cls, retryable = classify(status, exc, dict(resp.headers) if resp is not None else None, snippet)
            rl = {}
            if resp is not None and "X-RateLimit-Limit" in resp.headers:
                rl = {"rl_limit": resp.headers.get("X-RateLimit-Limit"),
                      "rl_remaining": resp.headers.get("X-RateLimit-Remaining"),
                      "rl_used": resp.headers.get("X-RateLimit-Used"),
                      "rl_resource": resp.headers.get("X-RateLimit-Resource"),
                      "rl_reset": resp.headers.get("X-RateLimit-Reset")}
            entry = {"source": source, "host": host, "url": log_url, "status": status, "class": cls,
                     "attempt": attempt, "ms": ms, "bytes": len(body), "wire_bytes": wire,
                     "conditional": bool(cond_headers), "error": type(exc).__name__ if exc else None,
                     "redirected": bool(resp is not None and resp.history), **rl}
            result.status, result.response, result.error_class = status, resp, cls

            if status is not None and (200 <= status < 300 or status == 304):
                self.ctx.log("request", **entry)
                br.record(True)
                if 200 <= status < 300:
                    self.etags.update(url, resp)
                    if cacheable and self.debug_cache:
                        self._cache_put(url, resp)
                return result

            if not retryable:
                self.ctx.log("request", level="WARNING", **entry)
                # 404/400 говорят об ошибке запроса, а не о болезни домена; 401/403 — сигнал доступа
                br.record(cls not in ("forbidden", "unauthorized"))
                return result

            state_before = br.state
            state = br.record(False)
            if state == "open" and state_before != "open":
                self.ctx.log("breaker_state", level="ERROR", host=host, state="open",
                             failures=br.failures, reset_after_s=br.reset_after_s)
            wait, why = server_requested_wait(dict(resp.headers) if resp is not None else {}, cls)
            if wait is None:
                wait = backoff_delay(attempt - 1, self.http["backoff_base_s"], self.http["backoff_cap_s"])
            if cls in ("rate_limited", "rate_limit_primary", "rate_limit_secondary", "server_error") \
                    and limiter_key in self.limiters:
                new_rate = self.limiters[limiter_key].penalize()
                entry["slowed_to_rate"] = round(new_rate, 3)
            entry.update({"retry_in_s": round(wait, 2), "wait_source": why})
            self.ctx.log("request", level="WARNING", **entry)
            if wait > float(self.http["max_retry_wait_s"]):
                br.trip()
                self.ctx.log("breaker_state", level="ERROR", host=host, state="open",
                             reason=f"сервер просит ждать {round(wait)} c > max_retry_wait_s")
                result.skipped = "wait_too_long"
                return result
            if attempt < max_attempts:
                time.sleep(wait)
        return result
