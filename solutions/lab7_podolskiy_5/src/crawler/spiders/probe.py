"""Паук-«зонд»: прогоняет список URL через тот же стек устойчивости (ограничитель,
повторы, предохранитель, условные запросы, детектор, JSON-журнал).

Нужен для воспроизведения кодов ответа на httpbin.org (реальный прогон) или на локальной
имитации: /status/429, /status/503, /status/401, /status/403, /status/404, /etag/<tag>,
/gzip, /delay/<с>, /response-headers?Retry-After=...
"""
from __future__ import annotations

import scrapy
from scrapy.exceptions import IgnoreRequest
from scrapy.spidermiddlewares.httperror import HttpError

from src.crawler.runtime import get_runtime, host_of
from src.retry import classify, parse_retry_after


class ProbeSpider(scrapy.Spider):
    name = "probe"
    handle_httpstatus_list = [304]

    def __init__(self, urls: str = "", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.urls = [u for u in urls.split("|") if u]

    async def start(self):
        for req in self.start_requests():
            yield req

    def start_requests(self):
        for i, url in enumerate(self.urls):
            yield scrapy.Request(url, callback=self.parse, errback=self.on_error, dont_filter=True,
                                 meta={"page_kind": "probe"}, priority=len(self.urls) - i)

    def parse(self, response):
        rt = get_runtime(self.crawler)
        ra = response.headers.get("Retry-After")
        ra_s = ra.decode("latin-1") if ra else None
        rt.log("probe_result", host=host_of(response.url), url=response.url, status=response.status,
               bytes=len(response.body), error_class=classify(response.status).error_class,
               retry_after_header=ra_s,
               retry_after_parsed_s=round(parse_retry_after(ra_s), 1) if ra_s and parse_retry_after(ra_s) is not None else None,
               content_encoding=(response.headers.get("Content-Encoding") or b"").decode() or None)

    def on_error(self, failure):
        rt = get_runtime(self.crawler)
        name = type(failure.value).__name__
        status = None
        if failure.check(HttpError):         # HttpError — подкласс IgnoreRequest, проверяем первым
            status = failure.value.response.status
            cls = classify(status).error_class
        elif name == "PolicyReschedule":     # вернётся в очередь по таймеру — это не результат
            return
        elif failure.check(IgnoreRequest):
            cls = "policy_deferred"          # отложен до следующего прогона / домен остановлен
        else:
            cls = classify(None, name).error_class
        rt.log("probe_result", host=host_of(failure.request.url), url=failure.request.url, status=status,
               error=name, error_class=cls, detail=str(failure.value)[:160])
