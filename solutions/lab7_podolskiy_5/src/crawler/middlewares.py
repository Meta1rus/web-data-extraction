"""Downloader middleware устойчивого пайплайна (лаб. 7).

ResilienceMiddleware (950)  — ограничитель скорости по доменам (token bucket + адаптивная
                              задержка + Crawl-delay + пауза после 429/503), предохранитель,
                              политика повторов, журнал КАЖДОЙ сетевой попытки.
ConditionalRequestMiddleware (940) — ETag/Last-Modified: запоминает и отправляет
                              If-None-Match / If-Modified-Since, учитывает ответы 304.
BlockDetectorMiddleware (545) — вердикт ok/подозрение/блокировка по коду И содержимому,
                              артефакты инцидента, остановка домена.

Ожидание делается асинхронно (deferLater): цикл событий не блокируется, time.sleep нет.
"""
from __future__ import annotations

import random
import time

from scrapy import signals
from scrapy.exceptions import IgnoreRequest
from scrapy.utils.defer import deferred_from_coro, maybe_deferred_to_future
from twisted.internet import task

from src.detector import BLOCK, OK, inspect, save_incident
from src.retry import classify, parse_retry_after
from src.crawler.runtime import get_runtime, host_of

BREAKER_FAILURE_STATUS = {500, 502, 503, 504, 522, 524}


class PolicyIgnore(IgnoreRequest):
    """Запрос сознательно не отправлен политикой и отложен до следующего прогона (state/deferred.json)
    или отброшен (блокировка домена, лимит запросов прогона)."""


class PolicyReschedule(PolicyIgnore):
    """Запрос не отправлен сейчас, но вернётся в очередь по таймеру (пауза домена, предохранитель)."""


def _hdr(response, name: str) -> str | None:
    v = response.headers.get(name)
    return v.decode("latin-1") if isinstance(v, bytes) else v


def _close_spider(crawler, reason: str) -> None:
    engine = crawler.engine
    if hasattr(engine, "close_spider_async"):          # Scrapy >= 2.14
        deferred_from_coro(engine.close_spider_async(reason=reason))
    else:
        engine.close_spider(crawler.spider, reason)


# =============================================================================== Resilience
class ResilienceMiddleware:
    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def __init__(self, crawler):
        self.crawler = crawler
        self.rt = get_runtime(crawler)
        self.parked: dict[str, int] = {}       # сколько запросов домена сейчас ждут «на месте»
        self.park_max_s = 5.0                  # дольше — возврат в очередь по таймеру

    def _later(self, request, delay: float, reason: str, budget_left: float, message: str):
        """Вернуть запрос в очередь через delay секунд (или отложить до следующего прогона) и
        прервать текущую отправку соответствующим исключением."""
        rt = self.rt
        if delay >= budget_left or rt.closed or request.meta.get("lab7_no_reschedule"):
            rt.defer(request, f"{reason}: не укладывается в бюджет URL или перенос запрещён")
            raise PolicyIgnore(f"{message}; отложен до следующего прогона")
        new = request.replace(dont_filter=True)
        key = id(new)
        rt.pending[key] = new
        rt.log("reschedule", host=host_of(request.url), url=request.url, attempt=request.meta.get("lab7_attempt"),
               delay_s=round(delay, 3), reason=reason)
        self.crawler.stats.inc_value("lab7/rescheduled")

        def fire():
            req = rt.pending.pop(key, None)
            if req is not None and not rt.closed:
                self.crawler.engine.crawl(req)

        from twisted.internet import reactor
        reactor.callLater(delay, fire)
        raise PolicyReschedule(f"{message}; повтор через {delay:.1f} с")

    async def process_request(self, request, spider=None):
        rt, host, meta = self.rt, host_of(request.url), request.meta
        meta.setdefault("lab7_attempt", 1)
        meta.setdefault("lab7_first_ts", time.monotonic())

        if rt.max_requests is not None and rt.sent >= rt.max_requests:
            rt.log("cap_reached", host=host, url=request.url, limit=rt.max_requests)
            raise PolicyIgnore(f"достигнут лимит запросов прогона ({rt.max_requests})")
        if host in rt.blocked:
            rt.defer(request, "domain_blocked")
            raise PolicyIgnore(f"домен {host} остановлен: {rt.blocked[host]}")

        now = time.monotonic()
        budget = rt.retry.budget_s if rt.retry is not None else 90.0
        budget_left = budget - (now - meta["lab7_first_ts"])

        probe = False
        if rt.breakers is not None:
            br = rt.breakers.get(host)
            if not br.allow():
                # open — ждать до пробного запроса; half-open — дождаться результата пробного
                when = (br.retry_at() - now) if br.state == "open" else br.current_reset / 2
                when = max(0.5, when) + random.uniform(0, 0.5)
                rt.log("breaker_reject", host=host, url=request.url, attempt=meta["lab7_attempt"],
                       state=br.state, retry_in_s=round(when, 2),
                       action="перенос" if when < budget_left else "отложен до следующего прогона")
                self._later(request, when, f"breaker_{br.state}", budget_left,
                            f"предохранитель домена {host}: {br.state}")
            probe = br.state == "half-open"
            meta["lab7_probe"] = probe

        wait_retry = max(0.0, meta.get("lab7_not_before", 0.0) - now)
        ahead = rt.limiter.peek(host) if rt.limiter is not None else 0.0
        wait = max(wait_retry, ahead)
        if wait > self.park_max_s:
            # Длинное ожидание (пауза после 429/503, Retry-After, медленный домен) не держит слот
            # загрузчика: запрос возвращается в очередь по таймеру, остальные домены в это время
            # обрабатываются (изоляция отказа). Короткие паузы темпа ждём на месте — так очередь
            # остаётся в планировщике (и в JOBDIR при прерывании).
            if probe:
                rt.breakers.get(host).probe_in_flight = False
            # очередь ожидающих этого домена: каждый следующий — на интервал позже (без «стада»)
            queued = sum(1 for r in rt.pending.values() if host_of(r.url) == host)
            interval = rt.limiter.policy(host).base_interval() if rt.limiter is not None else 0.5
            delay = wait + queued * min(interval, 1.0)   # при пробуждении ожидание пересчитается
            self._later(request, delay, "retry_delay" if wait_retry >= ahead else "rate_limit", budget_left,
                        f"ожидание {wait:.1f} с")

        reason = "retry_delay" if wait_retry > 0 else "none"
        if rt.limiter is not None:
            shared = rt.shared_bucket(host)
            w2, r2 = rt.limiter.reserve(host)            # bucket + адаптивная задержка + Crawl-delay
            if shared is not None:                       # общий счётчик для нескольких процессов
                w3 = shared.reserve()
                if w3 > w2:
                    w2, r2 = w3, "shared_token_bucket"
            if w2 > wait_retry:
                wait, reason = w2, r2
            else:
                wait = wait_retry
        if wait > 0:
            rt.log("limiter_wait", host=host, url=request.url, attempt=meta["lab7_attempt"],
                   wait_ms=round(wait * 1000, 1), reason=reason)
            from twisted.internet import reactor     # установленный Scrapy реактор (asyncio)
            self.parked[host] = self.parked.get(host, 0) + 1
            try:
                await maybe_deferred_to_future(task.deferLater(reactor, wait, lambda: None))
            finally:
                self.parked[host] -= 1

        meta["lab7_t_sent"] = time.monotonic()
        rt.sent += 1
        return None

    def _retry_or_giveup(self, request, status, retry_after, exc_name):
        rt, meta = self.rt, request.meta
        attempt = meta.get("lab7_attempt", 1)
        elapsed = time.monotonic() - meta.get("lab7_first_ts", time.monotonic())
        delay, source, why = rt.retry.next_delay(attempt, status, retry_after, elapsed, exc=exc_name)
        host = host_of(request.url)
        if delay is None:
            rt.log("giveup", host=host, url=request.url, status=status, attempt=attempt, error=exc_name,
                   reason=why, delay_source=source)
            rt.defer(request, f"giveup: {why}")
            return None
        rt.log("retry", host=host, url=request.url, status=status, attempt=attempt, error=exc_name,
               delay_s=round(delay, 3), delay_source=source, reason=why,
               retry_after_header=retry_after)
        new = request.replace(dont_filter=True)
        new.meta["lab7_attempt"] = attempt + 1
        new.meta["lab7_not_before"] = time.monotonic() + delay
        new.meta["lab7_first_ts"] = meta.get("lab7_first_ts")
        self.crawler.stats.inc_value("lab7/retries")
        return new

    def process_response(self, request, response, spider=None):
        rt, host, meta = self.rt, host_of(request.url), request.meta
        t_sent = meta.get("lab7_t_sent", time.monotonic())
        total = time.monotonic() - t_sent                  # с учётом очереди слота Scrapy
        latency = meta.get("download_latency") or total     # чистая латентность сервера
        status = response.status
        rt.log("request", host=host, url=request.url, status=status, ms=round(latency * 1000, 1),
               queue_ms=round(max(0.0, total - latency) * 1000, 1),
               attempt=meta.get("lab7_attempt", 1), bytes=len(response.body),
               encoding=_hdr(response, "Content-Encoding"), kind=meta.get("page_kind"),
               conditional=bool(meta.get("lab7_conditional")), probe=bool(meta.get("lab7_probe")))

        retry_after = _hdr(response, "Retry-After")
        if rt.limiter is not None:
            new_delay = rt.limiter.observe(host, latency, status)
            if status in (429, 503):
                ra = parse_retry_after(retry_after)
                pause = ra if ra is not None else (new_delay or 1.0)
                rt.limiter.penalize(host, pause)
                rt.log("domain_pause", host=host, url=request.url, status=status, pause_s=round(pause, 3),
                       source="retry-after" if ra is not None else "adaptive_delay",
                       adaptive_delay_s=round(new_delay, 3) if new_delay else None)

        if rt.breakers is not None:
            br = rt.breakers.get(host)
            if status in BREAKER_FAILURE_STATUS:
                br.record_failure(f"HTTP {status}")
            elif status != 429:
                br.record_success()

        if status == 401:
            rt.log("auth_required", host=host, url=request.url, status=401,
                   reason="нужна авторизация: сбор идёт без учётных данных, обновлять нечего — без повторов")
        if rt.retry is not None and classify(status).retry:
            new = self._retry_or_giveup(request, status, retry_after, None)
            if new is not None:
                return new
        return response

    def process_exception(self, request, exception, spider=None):
        if isinstance(exception, IgnoreRequest):
            return None
        rt, host, meta = self.rt, host_of(request.url), request.meta
        t_sent = meta.get("lab7_t_sent", time.monotonic())
        name = type(exception).__name__
        rt.log("request", host=host, url=request.url, status=None, ms=round((time.monotonic() - t_sent) * 1000, 1),
               attempt=meta.get("lab7_attempt", 1), error=name, kind=meta.get("page_kind"))
        if rt.breakers is not None:
            rt.breakers.get(host).record_failure(name)
        if rt.retry is not None:
            return self._retry_or_giveup(request, None, None, name)
        return None


# =============================================================================== Conditional
class ConditionalRequestMiddleware:
    """ETag / Last-Modified между прогонами (state/etags.json) → ответы 304 без тела."""

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def __init__(self, crawler):
        self.crawler = crawler
        self.rt = get_runtime(crawler)

    def process_request(self, request, spider=None):
        rt = self.rt
        if not rt.cond_send or request.url.endswith("/robots.txt") or request.meta.get("lab7_no_conditional"):
            return None
        saved = rt.etags.get(request.url)
        if not saved:
            return None
        if saved.get("etag") and b"If-None-Match" not in request.headers:
            request.headers["If-None-Match"] = saved["etag"]
        if saved.get("last_modified") and b"If-Modified-Since" not in request.headers:
            request.headers["If-Modified-Since"] = saved["last_modified"]
        request.meta["lab7_conditional"] = True
        return None

    def process_response(self, request, response, spider=None):
        rt = self.rt
        if response.status == 304:
            rt.log("not_modified", host=host_of(request.url), url=request.url, status=304,
                   bytes=len(response.body), saved_bytes=rt.etags.get(request.url, {}).get("bytes"))
            self.crawler.stats.inc_value("lab7/not_modified")
        elif response.status == 200 and rt.cond_record and not request.url.endswith("/robots.txt"):
            etag, lm = _hdr(response, "ETag"), _hdr(response, "Last-Modified")
            if etag or lm:
                rt.etags[request.url] = {"etag": etag, "last_modified": lm, "bytes": len(response.body),
                                         "saved_by_run": rt.run_id}
        return response


# =============================================================================== Detector
class BlockDetectorMiddleware:
    """Проверка содержимого ответа: маркеры заглушек, размер, наличие ожидаемых элементов."""

    EXPECT = {"list": "ul.products li.product, li.type-product",
              "product": "h1.product_title, div.summary h1"}

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def __init__(self, crawler):
        self.crawler = crawler
        self.rt = get_runtime(crawler)

    def process_response(self, request, response, spider=None):
        rt = self.rt
        if not rt.detector_enabled or request.url.endswith("/robots.txt"):
            return response
        host, kind, status = host_of(request.url), request.meta.get("page_kind"), response.status
        if status == 304:
            return response
        if status == 429:                    # ограничение скорости: реакция — у ограничителя и повторов
            self.crawler.stats.inc_value("lab7/verdict/подозрение")
            rt.log("rate_limited", host=host, url=request.url, status=429, bytes=len(response.body),
                   verdict="подозрение", signals=["код 429"])
            return response
        if status == 403:
            rt.consecutive_403[host] = rt.consecutive_403.get(host, 0) + 1
        else:
            rt.consecutive_403[host] = 0
        expected_found = None
        is_html = b"html" in (response.headers.get("Content-Type") or b"").lower()
        if status == 200 and kind in self.EXPECT and hasattr(response, "css"):
            expected_found = bool(response.css(self.EXPECT[kind]))
        verdict = inspect(status, response.body, expected_found=expected_found,
                          min_bytes=rt.min_bytes if kind in self.EXPECT else 0,
                          consecutive_403=rt.consecutive_403[host], is_html=is_html)
        self.crawler.stats.inc_value(f"lab7/verdict/{verdict.level}")
        if verdict.level == OK:
            return response
        rt.incident_seq += 1
        headers = {k.decode("latin-1"): [v.decode("latin-1") for v in vals]
                   for k, vals in response.headers.items()}
        path = save_incident(rt.incidents_dir, rt.run_id, rt.incident_seq, url=request.url, status=status,
                             headers=headers, body=response.body, verdict=verdict,
                             extra={"page_kind": kind, "mode": rt.mode})
        rt.log("incident", host=host, url=request.url, status=status, bytes=len(response.body),
               verdict=verdict.level, signals=verdict.signals, artifacts=str(path))
        if verdict.level == BLOCK:
            rt.blocked[host] = "; ".join(verdict.signals)
            rt.log("domain_blocked", host=host, url=request.url, status=status,
                   reason="вердикт «блокировка»: сбор домена остановлен, скорость не повышаем, защиту не обходим")
            if not rt.start_hosts or rt.start_hosts <= set(rt.blocked):
                _close_spider(self.crawler, "blocked")
            raise PolicyIgnore(f"блокировка: {verdict.signals}")
        return response
