"""Полный прогон после сбора: raw → clean → БД → метрики → mart (+ демонстрации для отчёта).

    python -m src.pipeline                 # всё по порядку
    python -m src.pipeline --fill-report   # и подставить цифры в report.md

Коды выхода: 0 — успех; 2 — «светофор» красный, mart не обновлён; 1 — ошибка.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np
import pandas as pd

from . import clean, config, contract, duckdb_bench, formats, mart, quality, rawio, rebuild

sys.path.insert(0, str(config.PROJECT_ROOT))
from db import load as dbload  # noqa: E402


def _payload(row: pd.Series) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, np.ndarray):
            out[k] = v.tolist()
        elif isinstance(v, pd.Timestamp):
            out[k] = v.to_pydatetime()
        elif v is None or (not isinstance(v, (list, str)) and pd.isna(v)):
            out[k] = None
        elif hasattr(v, "item"):
            out[k] = v.item()
        else:
            out[k] = v
    return out


def contract_probes() -> pd.DataFrame:
    events = pd.read_parquet(config.CLEAN_EVENTS)
    repos = pd.read_parquet(config.CLEAN_REPOS)
    base = events[(events["event_type"] == "issue") & (events["state"] == "closed")]
    ev = _payload(base.iloc[0]) if len(base) else None
    rp = _payload(repos.iloc[0]) if len(repos) else None
    df = pd.DataFrame(contract.run_probes(ev, rp))
    df.to_csv(config.LOG_DIR / "contract_probes.csv", index=False, encoding="utf-8")
    return df


def step(title: str) -> float:
    print(f"\n=== {title} ===", flush=True)
    return time.perf_counter()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fill-report", action="store_true")
    ap.add_argument("--skip-demo", action="store_true", help="без трёх демонстрационных прогонов загрузки")
    args = ap.parse_args(argv)
    config.ensure_dirs()
    timings = {}

    t = step("1. Проверка неизменности raw")
    for c in rawio.verify_all():
        print(f"  {c['file']}: {'OK' if c['checked'] else c.get('reason')}")
    timings["raw_verify"] = time.perf_counter() - t

    t = step("2. Очистка raw → clean (6 шагов + контракт)")
    res = clean.build()
    summary = clean.save(res)
    print(res.protocol.frame()[["шаг", "сущность", "строк до", "строк после", "в rejects"]].to_string(index=False))
    print(f"  clean: событий {summary['clean']['events']}, репозиториев {summary['clean']['repositories']}, "
          f"rejects {summary['rejects']}")
    timings["clean"] = time.perf_counter() - t

    t = step("3. Пересборка clean из raw без сети (проверка детерминированности)")
    rb = rebuild.rebuild(verify=True)
    print(f"  идентично: {rb['identical']}, {rb['seconds']} с")
    timings["rebuild"] = time.perf_counter() - t

    t = step("4. Сравнение форматов")
    bench, meta = formats.run()
    bench.to_csv(config.FORMAT_BENCH, index=False, encoding="utf-8")
    (config.LOG_DIR / "format_benchmark_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                               encoding="utf-8")
    print(bench[["формат", "размер, КБ", "чтение всего, мс", "чтение 3 столбцов, мс"]].to_string(index=False))
    timings["formats"] = time.perf_counter() - t

    t = step("5. Контракт на заведомо битых записях")
    probes = contract_probes()
    print(f"  проверок: {len(probes)}, отклонено: {(probes['результат'] == 'отклонена').sum()}")
    timings["probes"] = time.perf_counter() - t

    t = step("6. Загрузка в warehouse.db (UPSERT + SCD2)")
    conn = dbload.connect(config.WAREHOUSE_DB)
    r_db, e_db = dbload.read_clean()
    res_load = dbload.load(conn, r_db, e_db, label="pipeline")
    conn.close()
    print(f"  repositories: {res_load['repositories']}; events: {res_load['events']}")
    (config.LOG_DIR / "load_latest.json").write_text(json.dumps(res_load, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
    timings["load"] = time.perf_counter() - t

    if not args.skip_demo:
        t = step("7. Три прогона загрузки и отказ ограничений (db/demo_idempotency.db)")
        demo = dbload.demo()
        print(pd.DataFrame(demo["runs"])[["прогон", "repos: обновлено", "events: обновлено",
                                          "строк events", "новых версий в истории"]].to_string(index=False))
        timings["load_demo"] = time.perf_counter() - t

    t = step("8. Метрики качества и светофор")
    report = quality.run(record=True)
    print(report["table"][["метрика", "значение", "статус"]].to_string(index=False))
    print(f"  Вердикт: {report['verdict']}")
    red = quality.demo_red_gate()
    (config.LOG_DIR / "red_gate_demo.json").write_text(json.dumps(
        {"scenario": "HTML-парсер перестал находить данные (все поля страницы пустые, релизов нет)",
         "verdict": red["report"]["verdict"], "gate": red["gate"],
         "statuses": dict(zip(red["report"]["table"]["метрика"], red["report"]["table"]["статус"]))},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Демонстрация стоп-крана: {red['gate']}")
    timings["quality"] = time.perf_counter() - t

    t = step("9. Слой mart")
    code = 0
    try:
        out = mart.build(report)
        print(f"  представления: {out['views']}; партиции: {out['partitions']}; графики: {out['charts']}")
    except quality.QualityGateError as exc:
        print("  ⛔", exc)
        code = 2
    timings["mart"] = time.perf_counter() - t

    if code == 0:
        t = step("10. DuckDB по Parquet против SQLite (задание повышенной сложности)")
        print(duckdb_bench.run().to_string(index=False))
        timings["duckdb"] = time.perf_counter() - t

    (config.LOG_DIR / "pipeline_timings.json").write_text(
        json.dumps({k: round(v, 2) for k, v in timings.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.fill_report:
        from . import fill_report
        fill_report.main([])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
