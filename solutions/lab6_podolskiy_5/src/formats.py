"""Сравнение форматов хранения на своём наборе: CSV, JSON Lines (+gzip), Parquet.

Замеряется: размер файла, время полного чтения, время чтения трёх столбцов
(медиана из N повторов), типы после обратного чтения и судьба «трудных» значений:
* created_at — дата UTC;
* label_color — строка, которая может начинаться с нуля («008672»);
* commit_sha — короткий SHA, тоже может начинаться с нуля («0f3d138»);
* comments — целое с пропусками (у релизов комментариев нет);
* labels — список.
Запуск: python -m src.formats   → clean/format_benchmark.csv
"""
from __future__ import annotations

import statistics
import tempfile
import time
from pathlib import Path

import pandas as pd

from . import config

COLS3 = ["event_key", "created_at", "label_color"]


def _timeit(fn, repeats: int) -> float:
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times) * 1000


def _probe_value(df: pd.DataFrame, key: str | None, col: str):
    if key is None or col not in df.columns:
        return None
    hit = df.loc[df["event_key"] == key, col]
    if hit.empty:
        return None
    v = hit.iloc[0]
    return v.item() if hasattr(v, "item") else v      # numpy-скаляр → обычное значение Python


def run(events: pd.DataFrame | None = None, repeats: int = 7) -> tuple[pd.DataFrame, dict]:
    df = events if events is not None else pd.read_parquet(config.CLEAN_EVENTS)
    # «эталонные» строки для проверки сохранности значений
    zero_color = df.loc[df["label_color"].fillna("").str.match(r"^0\d{5}$"), "event_key"]
    zero_sha = df.loc[df["commit_sha"].fillna("").str.startswith("0"), "event_key"]
    key_color = zero_color.iloc[0] if len(zero_color) else None
    key_sha = zero_sha.iloc[0] if len(zero_sha) else None
    expected = {"label_color": _probe_value(df, key_color, "label_color"),
                "commit_sha": _probe_value(df, key_sha, "commit_sha")}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        paths = {"CSV": tmp / "events.csv", "JSON Lines": tmp / "events.jsonl",
                 "JSON Lines + gzip": tmp / "events.jsonl.gz", "Parquet (zstd)": tmp / "events.parquet"}
        df.to_csv(paths["CSV"], index=False)
        df.to_json(paths["JSON Lines"], orient="records", lines=True, force_ascii=False, date_format="iso")
        df.to_json(paths["JSON Lines + gzip"], orient="records", lines=True, force_ascii=False,
                   date_format="iso", compression="gzip")
        df.to_parquet(paths["Parquet (zstd)"], index=False, compression="zstd")

        readers = {
            "CSV": (lambda p: pd.read_csv(p), lambda p: pd.read_csv(p, usecols=COLS3)),
            "JSON Lines": (lambda p: pd.read_json(p, lines=True),
                           lambda p: pd.read_json(p, lines=True)[COLS3]),
            "JSON Lines + gzip": (lambda p: pd.read_json(p, lines=True, compression="gzip"),
                                  lambda p: pd.read_json(p, lines=True, compression="gzip")[COLS3]),
            "Parquet (zstd)": (lambda p: pd.read_parquet(p), lambda p: pd.read_parquet(p, columns=COLS3)),
        }
        rows = []
        for name, path in paths.items():
            full_reader, cols_reader = readers[name]
            back = full_reader(path)
            rows.append({
                "формат": name,
                "строк": len(back),
                "размер, КБ": round(path.stat().st_size / 1024, 1),
                "чтение всего, мс": round(_timeit(lambda: full_reader(path), repeats), 1),
                "чтение 3 столбцов, мс": round(_timeit(lambda: cols_reader(path), repeats), 1),
                "тип created_at": str(back["created_at"].dtype),
                "тип label_color": str(back["label_color"].dtype),
                f"label_color «{expected['label_color']}» →": repr(_probe_value(back, key_color, "label_color")),
                f"commit_sha «{expected['commit_sha']}» →": repr(_probe_value(back, key_sha, "commit_sha")),
                "тип comments (Int64 с пропусками)": str(back["comments"].dtype),
                "тип labels (список)": type(back["labels"].dropna().iloc[0]).__name__ if back["labels"].notna().any() else "—",
            })

        # Ловушка CSV: тип столбца выводится по ВСЕМ значениям партии. Если в партии все цвета —
        # цифры (например, только «help wanted» = 008672), ведущие нули теряются.
        batch = df[df["label_color"].fillna("").str.fullmatch(r"\d{6}")][["event_key", "label_color"]]
        trap = {}
        if len(batch):
            p = tmp / "digits_only.csv"
            batch.to_csv(p, index=False)
            back = pd.read_csv(p)
            trap = {"строк в партии": len(batch), "было": batch["label_color"].iloc[0],
                    "стало после CSV": repr(getattr(back["label_color"].iloc[0], "item", lambda: back["label_color"].iloc[0])()), "тип": str(back["label_color"].dtype),
                    "с dtype=str": repr(pd.read_csv(p, dtype={"label_color": str})["label_color"].iloc[0])}
    bench = pd.DataFrame(rows)
    csv_kb = bench.loc[bench["формат"] == "CSV", "размер, КБ"].iloc[0]
    pq_kb = bench.loc[bench["формат"] == "Parquet (zstd)", "размер, КБ"].iloc[0]
    meta = {"rows": len(df), "repeats": repeats, "csv_to_parquet_size_ratio": round(csv_kb / pq_kb, 1),
            "probe_keys": {"label_color": key_color, "commit_sha": key_sha}, "csv_digits_only_batch": trap,
            "pandas": pd.__version__}
    return bench, meta


def main() -> None:
    bench, meta = run()
    config.CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    bench.to_csv(config.FORMAT_BENCH, index=False, encoding="utf-8")
    import json
    (config.LOG_DIR / "format_benchmark_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                               encoding="utf-8")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 20)
    print(bench.T.to_string())
    print(f"\nParquet меньше CSV в {meta['csv_to_parquet_size_ratio']} раза; строк в наборе: {meta['rows']}")
    if meta["csv_digits_only_batch"]:
        print("Ловушка CSV на партии из одних цифр:", meta["csv_digits_only_batch"])


if __name__ == "__main__":
    main()
