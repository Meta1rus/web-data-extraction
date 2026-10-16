"""Доработанный сборщик (лаб. 7, вариант 5 — «Scrapy-краулер»).

Краулер лабораторной № 5 (каталог WooCommerce → карточка) + устойчивость:
token bucket по доменам, адаптивная задержка, robots.txt (Crawl-delay в приоритете),
политика повторов, предохранитель, условные запросы, gzip, инкрементальность,
детектор блокировок, структурные JSON-логи, отчёт о прогоне с порогами.

Режимы (п. 4.7):
  A — «наивный»: без лимита, без повторов, без условных запросов (robots.txt соблюдается ВСЕГДА);
  B — вежливый: token bucket + адаптивная задержка + AutoThrottle + повторы + предохранитель;
  C — B + условные запросы (ETag/Last-Modified) + gzip + инкрементальность («водяной знак»).

Примеры (из корня лабораторной):
  python -m src.collect --mode B --pages 1 --label B
  python -m src.collect --mode A --pages 1 --label A --max-requests 30
  python -m src.collect --robots-only --start-url https://www.scrapingcourse.com/ecommerce/
  python -m src.collect --mode B --pages 3 --label resume --jobdir jobstate/resume

Коды возврата: 0 — статус зелёный/жёлтый; 1 — красный; 2 — детектор зафиксировал блокировку
(сбор домена остановлен); 3 — robots.txt запрещает обход (сбор не начат).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from scrapy.crawler import CrawlerProcess
from scrapy.settings import Settings

from src.limiter import origin_of, read_robots

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_START = "https://www.scrapingcourse.com/ecommerce/"
UA = "PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)"

MODE_SETTINGS = {
    "A": {"CONCURRENT_REQUESTS": 16, "CONCURRENT_REQUESTS_PER_DOMAIN": 16, "DOWNLOAD_DELAY": 0,
          "AUTOTHROTTLE_ENABLED": False, "COMPRESSION_ENABLED": False},
    "B": {"CONCURRENT_REQUESTS": 8, "CONCURRENT_REQUESTS_PER_DOMAIN": 2, "DOWNLOAD_DELAY": 0,
          "AUTOTHROTTLE_ENABLED": True, "COMPRESSION_ENABLED": False},
    "C": {"CONCURRENT_REQUESTS": 8, "CONCURRENT_REQUESTS_PER_DOMAIN": 2, "DOWNLOAD_DELAY": 0,
          "AUTOTHROTTLE_ENABLED": True, "COMPRESSION_ENABLED": True},
}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-url", default=DEFAULT_START)
    ap.add_argument("--mode", choices=["A", "B", "C"], default="B")
    ap.add_argument("--label", help="метка прогона: reports/run_report_<label>.json")
    ap.add_argument("--pages", type=int, default=1, help="страниц каталога (1 стр. = 1 + 16 запросов)")
    ap.add_argument("--max-requests", type=int, help="жёсткий предел запросов (режим A на реальном сайте: 30)")
    ap.add_argument("--rate", type=float, default=2.0, help="запросов/с на домен (token bucket)")
    ap.add_argument("--burst", type=int, default=2)
    ap.add_argument("--jitter", type=float, default=0.25, help="доля джиттера интервала (0.2–0.3)")
    ap.add_argument("--max-attempts", type=int, default=4)
    ap.add_argument("--budget", type=float, default=90.0, help="бюджет времени на URL, с")
    ap.add_argument("--breaker-threshold", type=int, default=3)
    ap.add_argument("--breaker-reset", type=float, default=10.0)
    ap.add_argument("--cond", dest="cond", action="store_true", default=None, help="условные запросы вкл.")
    ap.add_argument("--no-cond", dest="cond", action="store_false")
    ap.add_argument("--gzip", dest="gzip", action="store_true", default=None)
    ap.add_argument("--no-gzip", dest="gzip", action="store_false")
    ap.add_argument("--incremental", dest="incremental", action="store_true", default=None)
    ap.add_argument("--no-incremental", dest="incremental", action="store_false")
    ap.add_argument("--revalidate-after", type=float, default=86400.0,
                    help="через сколько секунд карточку с неизменным отпечатком всё-таки перепроверять")
    ap.add_argument("--fresh-state", action="store_true", help="удалить state/etags.json и watermark.json")
    ap.add_argument("--jobdir", help="каталог состояния Scrapy (возобновление после прерывания)")
    ap.add_argument("--resume-deferred", action="store_true", help="дозабрать URL из state/deferred.json")
    ap.add_argument("--extra-url", help="второй домен: URL, который запрашивается после каждой карточки")
    ap.add_argument("--extra-fail-url", help="второй домен: URL «аварии» на первые --extra-outage-s секунд")
    ap.add_argument("--extra-outage-s", type=float, default=0.0)
    ap.add_argument("--shared-limit-dir", help="общий межпроцессный счётчик (несколько воркеров)")
    ap.add_argument("--profile", help="ключ сравнения с историей (по умолчанию: хост+страницы+режим)")
    ap.add_argument("--no-history", action="store_true", help="не добавлять прогон в reports/history.jsonl")
    ap.add_argument("--local-mock", action="store_true", help="пометка: прогон на локальной имитации")
    ap.add_argument("--robots-only", action="store_true", help="только отчёт по robots.txt")
    ap.add_argument("--probe-urls", help="режим «зонда»: URL через «|» (httpbin.org/status/429 и т.п.)")
    ap.add_argument("--timeout", type=float, default=20.0, help="DOWNLOAD_TIMEOUT, с")
    ap.add_argument("--log-level", default="WARNING")
    return ap.parse_args(argv)


def robots_reports(args) -> list[dict]:
    if args.probe_urls:
        urls = [u for u in args.probe_urls.split("|") if u]
        origins = list(dict.fromkeys(origin_of(u) for u in urls))
        return [read_robots(o, UA, [u for u in urls if origin_of(u) == o]).as_dict() for o in origins]
    origins = [origin_of(args.start_url)]
    for u in (args.extra_url, args.extra_fail_url):
        if u and origin_of(u) not in origins:
            origins.append(origin_of(u))
    out = []
    base = args.start_url if args.start_url.endswith("/") else args.start_url + "/"
    for origin in origins:
        if origin == origin_of(args.start_url):
            probes = [args.start_url, urljoin(base, "page/2/")]
            if "/ecommerce" in args.start_url:
                probes.append(urljoin(origin, "/ecommerce/product/abominable-hoodie/"))
        else:
            probes = [u for u in (args.extra_url, args.extra_fail_url) if u and origin_of(u) == origin]
        out.append(read_robots(origin, UA, probes).as_dict())
    return out


def build_settings(args, run_id: str, label: str, robots: list[dict]) -> Settings:
    s = Settings()
    s.setmodule("src.crawler.settings", priority="project")
    for k, v in MODE_SETTINGS[args.mode].items():
        s.set(k, v, priority="cmdline")
    cond = args.cond if args.cond is not None else args.mode == "C"
    gz = args.gzip if args.gzip is not None else args.mode == "C"
    incr = args.incremental if args.incremental is not None else args.mode == "C"
    s.set("COMPRESSION_ENABLED", gz, priority="cmdline")
    s.set("LOG_LEVEL", args.log_level, priority="cmdline")
    s.set("DOWNLOAD_TIMEOUT", args.timeout, priority="cmdline")
    if args.jobdir:
        s.set("JOBDIR", str(ROOT / args.jobdir), priority="cmdline")

    per_domain = {}
    for rep in robots:
        host = urlsplit(rep["origin"]).netloc
        cfg = {}
        if rep.get("crawl_delay"):
            cfg["crawl_delay"] = rep["crawl_delay"]          # Crawl-delay важнее наших настроек
        if rep.get("request_rate"):
            req, sec = rep["request_rate"]
            cfg["rate"] = min(args.rate, req / sec)
        if cfg:
            per_domain[host] = cfg
    naive = args.mode == "A"
    host = urlsplit(args.start_url).netloc
    start_hosts = ([urlsplit(r["origin"]).netloc for r in robots] if args.probe_urls else [host])
    s.set("LAB7", {
        "run_id": run_id, "label": label, "mode": args.mode, "start_url": args.start_url, "pages": args.pages,
        "start_hosts": start_hosts,
        "profile": args.profile or f"{host}|pages={args.pages}|mode={args.mode}|cond={cond}|gzip={gz}|incr={incr}",
        "log_path": str(ROOT / "logs" / f"run_{run_id}.jsonl"),
        "reports_dir": str(ROOT / "reports"), "history_path": str(ROOT / "reports" / "history.jsonl"),
        "incidents_dir": str(ROOT / "logs" / "incidents"),
        "deferred_path": str(ROOT / "state" / "deferred.json"),
        "limiter": {"enabled": not naive, "rate": args.rate, "burst": args.burst, "jitter": args.jitter,
                    "adaptive": True, "per_domain": per_domain,
                    "shared_dir": args.shared_limit_dir},
        "retry": {"enabled": not naive, "params": {"max_attempts": args.max_attempts, "base": 0.5, "cap": 20.0,
                                                   "budget_s": args.budget, "retry_after_cap": 120.0}},
        "breaker": {"enabled": not naive, "fail_threshold": args.breaker_threshold,
                    "reset_after": args.breaker_reset, "max_reset": 120.0},
        "detector": {"enabled": True, "min_bytes": 1500},
        "conditional": {"send": cond, "record": not naive, "path": str(ROOT / "state" / "etags.json")},
        "incremental": {"enabled": incr, "path": str(ROOT / "state" / "watermark.json"),
                        "revalidate_after_s": args.revalidate_after},
        "write_state": not naive,
        "max_requests": args.max_requests,
        "robots": robots,
        "no_history": args.no_history,
        "local_mock": args.local_mock,
    }, priority="cmdline")
    return s


def print_report(report: dict) -> None:
    keys = ["mode", "requests", "status_counts", "error_rate", "share_429_403", "retries", "retry_sources",
            "latency_median_ms", "latency_p95_ms", "traffic_kb", "not_modified_304", "incremental_skipped",
            "breaker_activations", "breaker_rejected", "incidents", "items_scraped", "items_dropped",
            "deferred_urls", "duration_s", "status"]
    print(f"\n== Отчёт о прогоне {report.get('label')} ({report.get('run_id')})"
          + ("  [ЛОКАЛЬНАЯ ИМИТАЦИЯ]" if report.get("local_mock") else ""))
    for k in keys:
        print(f"  {k:22s} {report.get(k)}")
    for a in report.get("alerts", []):
        print(f"  ОПОВЕЩЕНИЕ [{a['уровень']}] {a['порог']} = {a['значение']}: {a['обоснование']}")


def main(argv=None) -> int:
    args = parse_args(argv)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = args.label or f"{args.mode}_{ts}"
    run_id = f"{label}_{ts}"

    robots = robots_reports(args)
    for rep in robots:
        print(f"robots.txt {rep['robots_url']}: HTTP {rep['status']}; Crawl-delay={rep['crawl_delay']}; "
              f"Request-rate={rep['request_rate']}; Sitemap={rep['sitemaps']}")
        for url, ok in rep["can_fetch"].items():
            print(f"   can_fetch {url}: {ok}  (urllib.robotparser={rep['can_fetch_urllib'][url]}, "
                  f"Protego={rep['can_fetch_protego'][url]})")
        print(f"   {rep['policy_note']}")
    if args.robots_only:
        (ROOT / "reports").mkdir(exist_ok=True)
        host = urlsplit(args.start_url).netloc.replace(":", "_")
        (ROOT / "reports" / f"robots_report_{host}.json").write_text(
            json.dumps(robots, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0 if all(all(r["can_fetch"].values()) for r in robots[:1]) else 3
    if not all(all(r["can_fetch"].values()) for r in (robots if args.probe_urls else robots[:1])):
        print("СТОП: robots.txt запрещает обход стартового URL — сбор не начинаем (код 3).")
        return 3

    if args.fresh_state:
        for name in ("etags.json", "watermark.json", "deferred.json"):
            (ROOT / "state" / name).unlink(missing_ok=True)
    if args.jobdir and not (ROOT / args.jobdir).exists():
        (ROOT / args.jobdir).mkdir(parents=True, exist_ok=True)

    from src.crawler.spiders.ecommerce import EcommerceSpider     # после настройки окружения
    from src.crawler.spiders.probe import ProbeSpider

    settings = build_settings(args, run_id, label, robots)
    process = CrawlerProcess(settings)
    if args.probe_urls:
        process.crawl(ProbeSpider, urls=args.probe_urls)
    else:
        process.crawl(EcommerceSpider, start_url=args.start_url, max_pages=args.pages, extra_url=args.extra_url,
                      extra_fail_url=args.extra_fail_url, extra_outage_s=args.extra_outage_s,
                      resume_deferred=args.resume_deferred)
    process.start()

    report_path = ROOT / "reports" / f"run_report_{label}.json"
    if not report_path.exists():
        print("Отчёт о прогоне не сформирован — см. журнал", file=sys.stderr)
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print_report(report)
    if report.get("incidents_block") or report.get("blocked_domains"):
        print("ИТОГ: блокировка — сбор домена остановлен, артефакты в logs/incidents/ (код 2)")
        return 2
    return 1 if report.get("status") == "красный" else 0


if __name__ == "__main__":
    sys.exit(main())
