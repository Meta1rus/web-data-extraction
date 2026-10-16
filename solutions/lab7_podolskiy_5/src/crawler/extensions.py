"""Расширение RunObserver: начало/конец прогона в JSON-журнале, задержка AutoThrottle по
каждому ответу, отброшенные записи, сохранение состояния и отчёт о прогоне с порогами.
"""
from __future__ import annotations

import json
from pathlib import Path

from scrapy import signals
from scrapy.exceptions import DontCloseSpider

from src.crawler.runtime import ROOT, get_runtime, host_of
from src.detector import append_history, compare_history, load_history
from src.observability import build_run_report, evaluate_alerts, read_log


class RunObserver:
    @classmethod
    def from_crawler(cls, crawler):
        ext = cls(crawler)
        crawler.signals.connect(ext.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(ext.spider_closed, signal=signals.spider_closed)
        crawler.signals.connect(ext.response_downloaded, signal=signals.response_downloaded)
        crawler.signals.connect(ext.item_dropped, signal=signals.item_dropped)
        crawler.signals.connect(ext.spider_idle, signal=signals.spider_idle)
        return ext

    def spider_idle(self, spider):
        """Пока есть перенесённые по таймеру запросы (пауза домена, предохранитель) — не закрываться."""
        if self.rt.pending and not self.rt.closed:
            raise DontCloseSpider

    def __init__(self, crawler):
        self.crawler = crawler
        self.rt = get_runtime(crawler)
        self.autothrottle = crawler.settings.getbool("AUTOTHROTTLE_ENABLED")

    def spider_opened(self, spider):
        rt, s = self.rt, self.crawler.settings
        rt.log("run_start", mode=rt.mode, label=rt.label, start_url=rt.cfg.get("start_url"),
               settings={k: s.get(k) for k in ("CONCURRENT_REQUESTS", "CONCURRENT_REQUESTS_PER_DOMAIN",
                                                "DOWNLOAD_DELAY", "AUTOTHROTTLE_ENABLED", "COMPRESSION_ENABLED",
                                                "ROBOTSTXT_OBEY", "JOBDIR")},
               lab7={k: rt.cfg.get(k) for k in ("limiter", "retry", "breaker", "conditional", "incremental",
                                                "max_requests")})
        for rep in rt.cfg.get("robots", []):
            rt.log("robots", host=rep.get("origin", "").split("://")[-1], url=rep.get("robots_url"),
                   status=rep.get("status"), crawl_delay=rep.get("crawl_delay"),
                   request_rate=rep.get("request_rate"), sitemaps=rep.get("sitemaps"),
                   can_fetch=rep.get("can_fetch"), note=rep.get("policy_note"))

    def response_downloaded(self, response, request, spider):
        if not self.autothrottle:
            return
        key = request.meta.get("download_slot")
        slot = self.crawler.engine.downloader.slots.get(key) if key else None
        latency = request.meta.get("download_latency")
        if slot is not None:
            self.rt.log("autothrottle", host=host_of(request.url), url=request.url, status=response.status,
                        ms=round(latency * 1000, 1) if latency is not None else None,
                        delay_ms=round(slot.delay * 1000, 1))

    def item_dropped(self, item, response, exception, spider):
        self.rt.log("item_dropped", host=host_of(response.url) if response is not None else None,
                    url=response.url if response is not None else None, error=str(exception)[:200])

    def spider_closed(self, spider, reason):
        rt = self.rt
        rt.closed = True
        for req in list(rt.pending.values()):         # не успели вернуться в очередь — в deferred.json
            rt.defer(req, "pending_at_close")
        rt.pending.clear()
        rt.save_state()
        stats = self.crawler.stats.get_stats()
        rt.log("run_end", reason=reason, items=stats.get("item_scraped_count", 0),
               dropped=stats.get("item_dropped_count", 0), deferred=len(rt.deferred))
        events = [e for e in read_log(rt.logger.path) if e.get("run_id") == rt.run_id]
        report = build_run_report(events, stats, extra={
            "label": rt.label, "mode": rt.mode, "close_reason": reason, "start_url": rt.cfg.get("start_url"),
            "pages": rt.cfg.get("pages"), "profile": rt.cfg.get("profile"),
            "deferred_urls": len(rt.deferred),
            "breakers": rt.breakers.summary() if rt.breakers else {},
            "robots": [{k: r.get(k) for k in ("origin", "status", "crawl_delay", "request_rate", "sitemaps",
                                              "can_fetch", "policy_note")} for r in rt.cfg.get("robots", [])],
            "log_file": str(Path(rt.logger.path).relative_to(ROOT)) if str(rt.logger.path).startswith(str(ROOT))
            else str(rt.logger.path),
            "local_mock": bool(rt.cfg.get("local_mock")),
            "scrapy_stats": {k: stats.get(k) for k in (
                "downloader/request_count", "downloader/response_bytes", "httpcompression/response_bytes",
                "dupefilter/filtered", "scheduler/enqueued/disk", "scheduler/dequeued/disk",
                "robotstxt/forbidden", "lab7/incremental_skipped", "lab7/not_modified", "items/not_modified",
                "lab7/deferred_resumed", "item_dropped_count") if stats.get(k) is not None},
        })
        history_path = Path(rt.cfg.get("history_path", ROOT / "reports" / "history.jsonl"))
        current = {"run_id": rt.run_id, "profile": rt.cfg.get("profile"), "items": report["items_scraped"],
                   "avg_bytes": report["avg_response_bytes"], "mode": rt.mode, "label": rt.label}
        cmp = compare_history(current, load_history(history_path), profile=rt.cfg.get("profile"))
        alerts, status = evaluate_alerts(report, cmp)
        report.update(history=cmp, alerts=alerts, status=status)
        if not rt.cfg.get("no_history"):
            append_history(history_path, current)
        out = Path(rt.cfg.get("reports_dir", ROOT / "reports")) / f"run_report_{rt.label}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        rt.log("report", status=report["status"], alerts=len(alerts), report_file=str(out))
        rt.logger.close()
