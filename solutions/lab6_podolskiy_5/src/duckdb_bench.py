"""Задание повышенной сложности: DuckDB прямо по Parquet чистого слоя против SQLite.

    python -m src.duckdb_bench      → logs/duckdb_bench.csv

Одни и те же три аналитических запроса выполняются (а) DuckDB по файлу
clean/dataset.parquet без загрузки в БД и (б) SQLite по таблице events в
warehouse.db. Время — медиана из N повторов; результаты сверяются.
"""
from __future__ import annotations

import sqlite3
import statistics
import time

import pandas as pd

from . import config

QUERIES = {
    "события по репозиторию и типу": (
        "SELECT repo_key, event_type, COUNT(*) AS n FROM {events} GROUP BY 1, 2 ORDER BY 1, 2",
        "SELECT repo_key, event_type, COUNT(*) AS n FROM events GROUP BY 1, 2 ORDER BY 1, 2"),
    "среднее время закрытия, ч": (
        "SELECT repo_key, ROUND(AVG(date_diff('second', created_at, closed_at)) / 3600.0, 1) AS h "
        "FROM {events} WHERE closed_at IS NOT NULL AND event_type <> 'release' GROUP BY 1 ORDER BY 1",
        "SELECT repo_key, ROUND(AVG((julianday(closed_at) - julianday(created_at)) * 86400) / 3600.0, 1) AS h "
        "FROM events WHERE closed_at IS NOT NULL AND event_type <> 'release' GROUP BY 1 ORDER BY 1"),
    "новые события по неделям": (
        "SELECT strftime(date_trunc('week', created_at AT TIME ZONE 'UTC'), '%Y-%m-%d') AS week, COUNT(*) AS n "
        "FROM {events} GROUP BY 1 ORDER BY 1",
        "SELECT date(created_at, 'weekday 0', '-6 days') AS week, COUNT(*) AS n FROM events GROUP BY 1 ORDER BY 1"),
}


def _median_ms(fn, repeats: int) -> float:
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return round(statistics.median(times) * 1000, 2)


def run(repeats: int = 15) -> pd.DataFrame:
    try:
        import duckdb
    except ImportError:
        print("duckdb не установлен — pip install duckdb")
        return pd.DataFrame()
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    parquet = str(config.CLEAN_EVENTS).replace("'", "''")
    sq = sqlite3.connect(config.WAREHOUSE_DB)
    rows = []
    for name, (dq, sql) in QUERIES.items():
        dq = dq.format(events=f"read_parquet('{parquet}')")
        a = con.execute(dq).df()
        b = pd.read_sql_query(sql, sq)
        same = len(a) == len(b) and a.iloc[:, -1].astype(float).round(1).tolist() == b.iloc[:, -1].astype(float).round(1).tolist()
        rows.append({"запрос": name, "строк результата": len(a),
                     "DuckDB по Parquet, мс": _median_ms(lambda: con.execute(dq).fetchall(), repeats),
                     "SQLite, мс": _median_ms(lambda: sq.execute(sql).fetchall(), repeats),
                     "результаты совпали": same})
    sq.close()
    con.close()
    df = pd.DataFrame(rows)
    df.to_csv(config.LOG_DIR / "duckdb_bench.csv", index=False, encoding="utf-8")
    return df


if __name__ == "__main__":
    print(run().to_string(index=False))
