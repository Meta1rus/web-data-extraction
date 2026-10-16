#!/usr/bin/env python3
"""Демонстрация п. 4.5 задания: возобновляемость (JOBDIR) и HTTP-кэш (HTTPCACHE).

JOBDIR:   прогон 1 останавливается предохранителем CLOSESPIDER_ITEMCOUNT, состояние
          (очередь + отпечатки посещённых запросов) остаётся в jobstate/; прогон 2
          продолжает с того же места — dupefilter/filtered показывает, сколько уже
          посещённых URL НЕ запрошено повторно.
HTTPCACHE: два одинаковых прогона с включённым кэшем: первый пишет ответы на диск
          (httpcache/store), второй читает их оттуда (httpcache/hit) и к сайту не ходит.

Все прогоны — подмножество каталога (--pages страниц), чтобы не нагружать площадку.
Демонстрационные данные пишутся в data/demo/ (отдельная БД), статистика каждого
прогона — в data/stats_<номер>_<метка>.json, сводка — в data/demo/demo_summary.{json,md}.

Пример:
  python3 tools/demo_resume_cache.py --start-url https://www.scrapingcourse.com/ecommerce/
  python3 tools/demo_resume_cache.py --start-url http://127.0.0.1:18505/ecommerce/ --fast   # имитация
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEMO = DATA / "demo"
JOBDIR = ROOT / "jobstate"


def latest_stats(label: str) -> dict:
    files = sorted(DATA.glob(f"stats_*_{label}.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"не найден файл статистики для прогона {label!r}")
    return json.loads(files[-1].read_text(encoding="utf-8")) | {"_file": files[-1].name}


def crawl(label: str, start_url: str, pages: int, settings: dict, fast: bool, timeout: int) -> tuple[float, dict]:
    cmd = [sys.executable, "-m", "scrapy", "crawl", "ecommerce",
           "-a", f"start_url={start_url}", "-a", f"max_pages={pages}", "-a", f"run_label={label}",
           # FEEDS заменяется целиком: основной data/items.jsonl демонстрация не трогает
           "-s", "FEEDS=" + json.dumps({str(DEMO / f"items_{label}.jsonl"): {"format": "jsonlines",
                                                                              "overwrite": True}}),
           "-s", f"SQLITE_PATH={DEMO / 'demo.db'}", "-s", "LOG_LEVEL=WARNING"]
    if fast:  # только для локальной имитации: на реальном сайте паузы не уменьшаем
        settings = {"DOWNLOAD_DELAY": 0.02, "AUTOTHROTTLE_START_DELAY": 0.02, **settings}
    for k, v in settings.items():
        cmd += ["-s", f"{k}={v}"]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    secs = round(time.perf_counter() - t0, 2)
    if proc.returncode != 0:
        print(proc.stdout[-3000:], proc.stderr[-3000:], sep="\n")
        raise SystemExit(f"прогон {label} завершился с кодом {proc.returncode}")
    return secs, latest_stats(label)


def skus(label: str) -> list[str]:
    path = DEMO / f"items_{label}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["sku"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def count_fingerprints(path: Path) -> int:
    """Число отпечатков в requests.seen.

    Scrapy ≥ 2.19 пишет файл бинарно: 2 байта длины + отпечаток; более ранние версии —
    hex-строками по одной на строку. Поддерживаем оба формата.
    """
    if not path.exists():
        return 0
    raw = path.read_bytes()
    pos, n = 0, 0
    while pos + 2 <= len(raw):
        size = int.from_bytes(raw[pos:pos + 2], "big")
        if size == 0 or pos + 2 + size > len(raw):
            break
        pos, n = pos + 2 + size, n + 1
    if pos == len(raw):
        return n
    return len([ln for ln in raw.splitlines() if ln.strip()])


def tree(path: Path) -> list[dict]:
    rows = []
    for p in sorted(path.rglob("*")):
        if p.is_file():
            rows.append({"файл": str(p.relative_to(ROOT)), "байт": p.stat().st_size})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-url", default="https://www.scrapingcourse.com/ecommerce/")
    ap.add_argument("--pages", type=int, default=4, help="страниц каталога в демонстрации JOBDIR")
    ap.add_argument("--first-items", type=int, default=30, help="CLOSESPIDER_ITEMCOUNT первого прогона")
    ap.add_argument("--cache-pages", type=int, default=3, help="страниц каталога в демонстрации кэша")
    ap.add_argument("--fast", action="store_true", help="уменьшить паузы (ТОЛЬКО для локальной имитации)")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    DEMO.mkdir(parents=True, exist_ok=True)
    (DEMO / "demo.db").unlink(missing_ok=True)
    summary: dict = {"start_url": args.start_url, "fast_mode_local_mock": args.fast}

    # ---------------------------------------------------------------- JOBDIR
    shutil.rmtree(JOBDIR, ignore_errors=True)
    t1, s1 = crawl("jobdir_run1", args.start_url, args.pages,
                   {"JOBDIR": JOBDIR, "CLOSESPIDER_ITEMCOUNT": args.first_items}, args.fast, args.timeout)
    state_files = tree(JOBDIR)
    fingerprints = count_fingerprints(JOBDIR / "requests.seen")
    t2, s2 = crawl("jobdir_run2", args.start_url, args.pages,
                   {"JOBDIR": JOBDIR, "CLOSESPIDER_ITEMCOUNT": 0}, args.fast, args.timeout)
    k1, k2 = skus("jobdir_run1"), skus("jobdir_run2")
    summary["jobdir"] = {
        "run1": {"секунд": t1, "close_reason": s1.get("close_reason"), "записей": s1.get("item_scraped_count", 0),
                 "запросов": s1.get("downloader/request_count", 0), "stats_file": s1["_file"]},
        "jobstate_files": state_files,
        "fingerprints_in_requests_seen": fingerprints,
        "run2": {"секунд": t2, "close_reason": s2.get("close_reason"), "записей": s2.get("item_scraped_count", 0),
                 "запросов": s2.get("downloader/request_count", 0),
                 "dupefilter/filtered": s2.get("dupefilter/filtered", 0), "stats_file": s2["_file"]},
        "unique_sku_total": len(set(k1) | set(k2)),
        "sku_repeated_in_run2": len(set(k1) & set(k2)),
    }

    # ---------------------------------------------------------------- HTTPCACHE
    cache_dir = DEMO / "httpcache"
    shutil.rmtree(cache_dir, ignore_errors=True)
    cache_settings = {"HTTPCACHE_ENABLED": True, "HTTPCACHE_DIR": cache_dir, "CLOSESPIDER_ITEMCOUNT": 0}
    c1_t, c1 = crawl("cache_run1", args.start_url, args.cache_pages, cache_settings, args.fast, args.timeout)
    c2_t, c2 = crawl("cache_run2", args.start_url, args.cache_pages, cache_settings, args.fast, args.timeout)
    summary["httpcache"] = [
        {"прогон": name, "секунд (wall)": t, "elapsed_time_seconds": round(s.get("elapsed_time_seconds", 0), 2),
         "записей": s.get("item_scraped_count", 0), "httpcache/store": s.get("httpcache/store", 0),
         "httpcache/hit": s.get("httpcache/hit", 0), "httpcache/miss": s.get("httpcache/miss", 0),
         "stats_file": s["_file"]}
        for name, t, s in (("cache_run1", c1_t, c1), ("cache_run2", c2_t, c2))
    ]

    (DEMO / "demo_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                                            encoding="utf-8")
    j = summary["jobdir"]
    md = [
        "### JOBDIR: прерывание и возобновление",
        "",
        "| Прогон | Причина остановки | Записей | Запросов | dupefilter/filtered | Время, с |",
        "|---|---|---|---|---|---|",
        f"| 1 (CLOSESPIDER_ITEMCOUNT={args.first_items}) | {j['run1']['close_reason']} | {j['run1']['записей']} | "
        f"{j['run1']['запросов']} | — | {j['run1']['секунд']} |",
        f"| 2 (возобновление из jobstate/) | {j['run2']['close_reason']} | {j['run2']['записей']} | "
        f"{j['run2']['запросов']} | {j['run2']['dupefilter/filtered']} | {j['run2']['секунд']} |",
        "",
        f"Отпечатков в jobstate/requests.seen после прогона 1: {j['fingerprints_in_requests_seen']}. "
        f"Уникальных SKU за два прогона: {j['unique_sku_total']}, повторно собранных во втором: "
        f"{j['sku_repeated_in_run2']}.",
        "",
        "Содержимое jobstate/ после прогона 1:",
        "",
        "| Файл | Байт |",
        "|---|---|",
        *[f"| {r['файл']} | {r['байт']} |" for r in j["jobstate_files"]],
        "",
        "### HTTPCACHE: два прогона подряд",
        "",
        "| Прогон | Время, с | Записей | httpcache/store | httpcache/hit | httpcache/miss |",
        "|---|---|---|---|---|---|",
        *[f"| {r['прогон']} | {r['секунд (wall)']} | {r['записей']} | {r['httpcache/store']} | "
          f"{r['httpcache/hit']} | {r['httpcache/miss']} |" for r in summary["httpcache"]],
    ]
    if args.fast:
        md.insert(0, "> Цифры получены на ЛОКАЛЬНОЙ ИМИТАЦИИ (--fast), не на реальном сайте.\n")
    (DEMO / "demo_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
