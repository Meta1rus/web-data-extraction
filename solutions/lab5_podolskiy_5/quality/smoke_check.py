#!/usr/bin/env python3
"""Smoke-check: сравнение текущего прогона с предыдущим (п. 4.6 задания).

Предупреждение выдаётся, если
  1) число записей упало более чем на 20 % относительно предыдущего прогона;
  2) доля пустых значений любого обязательного поля варианта превысила 5 %
     (поля: name, price, sku, categories, description, image_url; пустые
     значения считает звено 100 пайплайна → quality/empty/<поле>);
  3) (если указан --db) в базе среди записей текущего прогона доля пустых
     обязательных полей превысила 5 %.

Источник данных — файлы data/stats_<номер>_<метка>.json, которые пишет closed() паука.
Код возврата: 0 — норма, 1 — есть предупреждения (удобно для cron / n8n / CI).

Примеры:
  python3 quality/smoke_check.py                       # два последних прогона с меткой main*
  python3 quality/smoke_check.py --current data/stats_07_main.json --previous data/stats_05_main.json
  python3 quality/smoke_check.py --self-test           # проверка, что тревога срабатывает
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FIELDS = ["name", "price_raw", "sku", "categories", "description", "image_url"]
DB_FIELDS = {"name": "name", "price": "price", "sku": "sku", "categories": "categories",
             "description": "description", "image_url": "image_url"}
DROP_THRESHOLD = 0.20      # падение числа записей
EMPTY_THRESHOLD = 0.05     # доля пустых обязательных полей


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pick_runs(label_prefix: str) -> list[Path]:
    runs = []
    for p in DATA.glob("stats_*.json"):
        s = load(p)
        if str(s.get("run_label", "")).startswith(label_prefix):
            runs.append((s.get("run_no", 0), p))
    return [p for _, p in sorted(runs)]


def metrics(stats: dict) -> dict:
    seen = stats.get("quality/items_seen", 0) or 0
    m = {"items": stats.get("item_scraped_count", 0) or 0, "items_seen": seen,
         "dropped_missing": stats.get("dropped/missing_fields", 0) or 0}
    for f in FIELDS:
        m[f"empty/{f}"] = (stats.get(f"quality/empty/{f}", 0) or 0) / seen if seen else 0.0
    return m


def check(cur: dict, prev: dict | None) -> list[str]:
    warnings = []
    c = metrics(cur)
    if prev is not None:
        p = metrics(prev)
        if p["items"] > 0:
            drop = (p["items"] - c["items"]) / p["items"]
            status = "ПРЕДУПРЕЖДЕНИЕ" if drop > DROP_THRESHOLD else "ок"
            print(f"  записей: было {p['items']}, стало {c['items']} → изменение {-drop:+.1%} "
                  f"(порог −{DROP_THRESHOLD:.0%}) — {status}")
            if drop > DROP_THRESHOLD:
                warnings.append(f"число записей упало на {drop:.1%} (порог {DROP_THRESHOLD:.0%})")
    else:
        print(f"  записей: {c['items']} (предыдущего прогона для сравнения нет)")
    for f in FIELDS:
        share = c[f"empty/{f}"]
        status = "ПРЕДУПРЕЖДЕНИЕ" if share > EMPTY_THRESHOLD else "ок"
        print(f"  пустых «{f}»: {share:.1%} (порог {EMPTY_THRESHOLD:.0%}) — {status}")
        if share > EMPTY_THRESHOLD:
            warnings.append(f"доля пустых «{f}» {share:.1%} > {EMPTY_THRESHOLD:.0%}")
    return warnings


def check_db(db: Path, run_id: str) -> list[str]:
    warnings = []
    with sqlite3.connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM products WHERE last_seen_run = ?", (run_id,)).fetchone()[0]
        if not total:
            return [f"в БД нет записей прогона {run_id}"]
        for label, col in DB_FIELDS.items():
            empty = conn.execute(
                f"SELECT COUNT(*) FROM products WHERE last_seen_run = ? AND "
                f"({col} IS NULL OR {col} = '' OR {col} = '[]')", (run_id,)).fetchone()[0]
            share = empty / total
            print(f"  БД: пустых «{label}» {empty}/{total} = {share:.1%}")
            if share > EMPTY_THRESHOLD:
                warnings.append(f"БД: доля пустых «{label}» {share:.1%} > {EMPTY_THRESHOLD:.0%}")
    return warnings


def self_test(base: dict) -> int:
    """Синтетическая деградация: −30 % записей и 8 % пустых описаний должны дать тревогу."""
    bad = dict(base)
    bad["item_scraped_count"] = int((base.get("item_scraped_count") or 100) * 0.7)
    seen = base.get("quality/items_seen") or 100
    bad["quality/empty/description"] = int(seen * 0.08) + 1
    print("Self-test: синтетический прогон (−30 % записей, 8 % пустых описаний)")
    warnings = check(bad, base)
    ok = any("упало" in w for w in warnings) and any("description" in w for w in warnings)
    print("Self-test:", "ПРОЙДЕН — тревога сработала" if ok else "НЕ ПРОЙДЕН")
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--current", type=Path)
    ap.add_argument("--previous", type=Path)
    ap.add_argument("--label", default="main", help="префикс метки прогонов для автоподбора")
    ap.add_argument("--db", type=Path, default=DATA / "crawl.db")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.current:
        cur_path, prev_path = args.current, args.previous
    else:
        runs = pick_runs(args.label)
        if not runs:
            print(f"Нет файлов статистики с меткой {args.label!r} в {DATA}")
            return 1
        cur_path = runs[-1]
        prev_path = args.previous or (runs[-2] if len(runs) > 1 else None)
    cur = load(cur_path)
    if args.self_test:
        return self_test(cur)

    print(f"Текущий прогон: {cur_path.name}" + (f"; предыдущий: {prev_path.name}" if prev_path else ""))
    warnings = check(cur, load(prev_path) if prev_path else None)
    if args.db and args.db.exists() and cur.get("run_id"):
        warnings += check_db(args.db, cur["run_id"])
    if warnings:
        print("ИТОГ: ПРЕДУПРЕЖДЕНИЕ —", "; ".join(warnings))
        return 1
    print("ИТОГ: норма")
    return 0


if __name__ == "__main__":
    sys.exit(main())
