#!/usr/bin/env python3
"""reports/comparison.md: таблица прогонов A/B/C и таблица экономии нагрузки по механизмам.

Берёт отчёты reports/run_report_<метка>.json. Реальный сайт — метки A, B, C, S0_base,
S1_cond, S2_gzip, S3_incr (их пишет run_real.sh); локальная имитация — те же метки с
префиксом mock_ (tools/run_mock_scenarios.py). Отсутствующие значения — ⟪ЗАПОЛНИТЬ⟫.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REP = ROOT / "reports"
FILL = "⟪ЗАПОЛНИТЬ⟫"


def load(label: str) -> dict | None:
    p = REP / f"run_report_{label}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def v(rep, key, fmt="{}"):
    if not rep or rep.get(key) is None:
        return FILL
    return fmt.format(rep[key])


def pct(before, after):
    if before in (None, 0) or after is None:
        return FILL
    return f"{(1 - after / before) * 100:.1f}"


def section(prefix: str, title: str) -> list[str]:
    a, b, c = load(prefix + "A"), load(prefix + "B"), load(prefix + "C")
    s0, s1, s2, s3 = (load(prefix + x) for x in ("S0_base", "S1_cond", "S2_gzip", "S3_incr"))
    lines = [f"## {title}", "",
             "### Прогоны A / B / C (одинаковый объём: 1 страница каталога + её карточки)", "",
             "| Прогон | Настройки | Время, с | Запросов | Трафик, КБ | Доля ошибок | 429/403 | Повторов | Ответов 304 | Пропущено (инкр.) | Записей |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, rep, desc in (("A", a, "без лимита, без повторов, без условных запросов"),
                            ("B", b, "token bucket + адаптивная задержка + AutoThrottle + повторы + предохранитель"),
                            ("C", c, "B + ETag/Last-Modified + gzip + «водяной знак» (повторный прогон после B)")):
        lines.append(f"| {name} | {desc} | {v(rep, 'duration_s')} | {v(rep, 'requests')} | {v(rep, 'traffic_kb')} | "
                     f"{v(rep, 'error_rate', '{:.1%}')} | {v(rep, 'share_429_403', '{:.1%}')} | {v(rep, 'retries')} | "
                     f"{v(rep, 'not_modified_304')} | {v(rep, 'incremental_skipped')} | {v(rep, 'items_scraped')} |")
    if a and b and a.get("duration_s") is not None and b.get("duration_s") is not None:
        lines += ["", f"Цена вежливости: B медленнее A на {b['duration_s'] - a['duration_s']:.1f} с "
                      f"({b['duration_s']:.1f} против {a['duration_s']:.1f} с); при этом A получил "
                      f"{a.get('status_counts', {}).get('429', 0)} ответов 429 и собрал {a.get('items_scraped')} записей "
                      f"против {b.get('items_scraped')} у B."]
    if b and c:
        lines.append(f"Выигрыш C относительно B: трафик {b['traffic_kb']} → {c['traffic_kb']} КБ "
                     f"(−{pct(b['traffic_kb'], c['traffic_kb'])} %), запросов {b['requests']} → {c['requests']}.")
    lines += ["", "### Экономия нагрузки по механизмам", "",
              "| Механизм | Сравнение | Запросов до | Запросов после | Трафик до, КБ | Трафик после, КБ | Экономия трафика, % | Экономия запросов, % |",
              "|---|---|---|---|---|---|---|---|"]
    rows = [("Условные запросы (ETag / Last-Modified → 304)", "S0 → S1", s0, s1),
            ("Сжатие (Accept-Encoding: gzip)", "S0 → S2", s0, s2),
            ("Инкрементальность («водяной знак» + 304 на каталоге)", "S1 → S3", s1, s3),
            ("Все механизмы вместе", "B → C", b, c)]
    for mech, cmp_, x, y in rows:
        lines.append(f"| {mech} | {cmp_} | {v(x, 'requests')} | {v(y, 'requests')} | {v(x, 'traffic_kb')} | "
                     f"{v(y, 'traffic_kb')} | {pct(x and x.get('traffic_kb'), y and y.get('traffic_kb'))} | "
                     f"{pct(x and x.get('requests'), y and y.get('requests'))} |")
    lines.append("")
    return lines


def main() -> None:
    lines = ["# Сравнение прогонов", "",
             "Трафик — байты тел ответов «на проводе» (до распаковки gzip), из JSON-журнала. "
             "Запросы — сетевые попытки, включая robots.txt и повторы.", ""]
    lines += section("", "Реальный сайт (согласованный учебный источник)")
    lines += section("mock_", "Локальная имитация (tools/mock_shop.py) — цифры только «(на локальной имитации)»")
    out = REP / "comparison.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
