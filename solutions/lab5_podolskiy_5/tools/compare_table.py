#!/usr/bin/env python3
"""Таблица сравнения Scrapy vs n8n (п. 4.8) из измерений.

Scrapy-часть берётся из статистики прогонов (data/stats_*_subset_n8n.json — то же
подмножество из 2 страниц каталога, что обрабатывает workflow, и data/stats_*_main.json).
n8n-часть: число записей считается по nocode/nocode_result.csv, время прогона и число
запросов берутся из nocode/n8n_metrics.json (журнал выполнений n8n: Executions → длительность).

Трафик n8n сам не сообщает: выводится ОЦЕНКА = число запросов × средний размер ответа,
измеренный Scrapy на тех же страницах (помечено «оценка»).

Результат печатается и сохраняется в nocode/comparison_table.md.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
NOCODE = ROOT / "nocode"
FILL = "⟪ЗАПОЛНИТЬ⟫"


def latest(label: str) -> dict | None:
    files = sorted(DATA.glob(f"stats_*_{label}.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else None


def fmt(v, nd=2):
    if v is None:
        return FILL
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def main() -> None:
    sub, main_run = latest("subset_n8n"), latest("main")
    metrics_path = NOCODE / "n8n_metrics.json"
    m = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
    n8n_rows = 0
    csv_path = NOCODE / "nocode_result.csv"
    if csv_path.exists():
        with csv_path.open(encoding="utf-8-sig") as f:      # n8n пишет CSV с BOM
            n8n_rows = sum(1 for _ in csv.DictReader(f))

    def scrapy_cols(s):
        if not s:
            return dict.fromkeys(["items", "secs", "ipm", "req", "mb"])
        secs = s.get("elapsed_time_seconds")
        items = s.get("item_scraped_count", 0)
        return {"items": items, "secs": secs, "ipm": items / secs * 60 if secs else None,
                "req": s.get("downloader/request_count"),
                "mb": s.get("downloader/response_bytes", 0) / 1_048_576}

    sc, sm = scrapy_cols(sub), scrapy_cols(main_run)
    n_secs = m.get("run_seconds")
    n_req = m.get("http_requests")
    avg_resp = (sub.get("downloader/response_bytes", 0) / sub.get("downloader/request_count", 1)) if sub else None
    n_mb = (n_req * avg_resp / 1_048_576) if (n_req and avg_resp) else None

    rows = [
        ("Записей собрано", fmt(sc["items"]), fmt(sm["items"]), fmt(n8n_rows or None)),
        ("Время прогона, с", fmt(sc["secs"], 1), fmt(sm["secs"], 1), fmt(n_secs, 1)),
        ("Записей в минуту", fmt(sc["ipm"], 1), fmt(sm["ipm"], 1),
         fmt(n8n_rows / n_secs * 60 if (n8n_rows and n_secs) else None, 1)),
        ("Число сетевых запросов", fmt(sc["req"]), fmt(sm["req"]), fmt(n_req)),
        ("Трафик, МБ", fmt(sc["mb"], 3), fmt(sm["mb"], 3),
         (fmt(n_mb, 3) + " (оценка)") if n_mb is not None else FILL),
        ("Дедупликация запросов", "dupefilter (отпечатки запросов) + DedupPipeline по SKU", "",
         "Remove Duplicates по URL и по SKU (в пределах одного выполнения)"),
        ("Возобновление после сбоя", "JOBDIR: очередь и отпечатки на диске", "",
         "нет: выполнение начинается заново (можно повторить с узла вручную)"),
        ("Расписание и оповещения", "внешний cron/Scrapyd; оповещения — свой код", "",
         "Schedule Trigger + ветка ошибки из коробки"),
        ("Версионирование логики", "git: код, настройки, тесты", "",
         "экспорт workflow.json в git; diff JSON малочитаем"),
    ]
    lines = ["| Метрика | Scrapy (подмножество, 2 стр.) | Scrapy (итоговый прогон) | n8n (2 стр.) |",
             "|---|---|---|---|"]
    lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    text = "\n".join(lines) + "\n"
    NOCODE.mkdir(exist_ok=True)
    (NOCODE / "comparison_table.md").write_text(text, encoding="utf-8")
    print(text)
    if not metrics_path.exists():
        print(f"Нет {metrics_path.name}: скопируйте n8n_metrics.example.json и впишите измерения n8n.")


if __name__ == "__main__":
    main()
