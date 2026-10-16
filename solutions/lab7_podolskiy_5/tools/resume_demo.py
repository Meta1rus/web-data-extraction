#!/usr/bin/env python3
"""Сценарий варианта 5 «прерывание и возобновление прогона».

Прогон 1 запускается с JOBDIR и через --after секунд получает SIGINT (как Ctrl+C):
Scrapy мягко останавливается и сохраняет очередь и отпечатки запросов; наш сборщик
сохраняет ETag, «водяной знак» и отложенные URL (state/). Прогон 2 с тем же JOBDIR
продолжает с места остановки: dupefilter/filtered показывает, что уже посещённые URL
повторно не запрашиваются; в БД записи обоих прогонов без дублей по SKU.

Пример:  python3 tools/resume_demo.py --start-url https://scrapeme.live/shop/ --pages 2 --prefix real_resume
"""
from __future__ import annotations

import argparse
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-url", required=True)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--after", type=float, default=8.0, help="через сколько секунд прервать прогон 1")
    ap.add_argument("--prefix", default="resume")
    ap.add_argument("--local-mock", action="store_true")
    args = ap.parse_args(argv)

    jobdir = f"jobstate/{args.prefix}"
    shutil.rmtree(ROOT / jobdir, ignore_errors=True)
    common = ["--mode", "B", "--pages", str(args.pages), "--start-url", args.start_url, "--jobdir", jobdir,
              "--no-history"] + (["--local-mock"] if args.local_mock else [])
    cmd1 = [sys.executable, "-m", "src.collect", "--label", f"{args.prefix}_1", "--fresh-state", *common]
    print("$", " ".join(cmd1[2:]), f"  (через {args.after} с — SIGINT)", flush=True)
    proc = subprocess.Popen(cmd1, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(args.after)
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    files = sorted(f"{p.relative_to(ROOT)} ({p.stat().st_size} Б)" for p in (ROOT / jobdir).rglob("*") if p.is_file())

    # --resume-deferred: URL, ждавшие по таймеру в момент прерывания, сохранены в state/deferred.json
    cmd2 = [sys.executable, "-m", "src.collect", "--label", f"{args.prefix}_2", "--resume-deferred", *common]
    print("$", " ".join(cmd2[2:]), flush=True)
    subprocess.run(cmd2, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)

    r1 = json.loads((ROOT / "reports" / f"run_report_{args.prefix}_1.json").read_text(encoding="utf-8"))
    r2 = json.loads((ROOT / "reports" / f"run_report_{args.prefix}_2.json").read_text(encoding="utf-8"))
    with sqlite3.connect(ROOT / "data" / "crawl.db") as conn:
        rows = conn.execute(
            "SELECT last_seen_run, COUNT(*) FROM products WHERE last_seen_run IN (?, ?) GROUP BY last_seen_run",
            (r1["run_id"], r2["run_id"])).fetchall()
    summary = {
        "local_mock": args.local_mock, "start_url": args.start_url, "pages": args.pages,
        "run1": {"close_reason": r1.get("close_reason"), "requests": r1.get("requests"),
                 "items": r1.get("items_scraped"), "scrapy_stats": r1.get("scrapy_stats")},
        "jobstate_files": files,
        "run2": {"close_reason": r2.get("close_reason"), "requests": r2.get("requests"),
                 "items": r2.get("items_scraped"), "dupefilter/filtered": (r2.get("scrapy_stats") or {}).get("dupefilter/filtered"),
                 "scrapy_stats": r2.get("scrapy_stats")},
        "db_rows_by_run": rows,
    }
    out = ROOT / "reports" / f"{args.prefix}_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("run1", "run2", "db_rows_by_run")}, ensure_ascii=False, indent=1))
    return summary


if __name__ == "__main__":
    main()
