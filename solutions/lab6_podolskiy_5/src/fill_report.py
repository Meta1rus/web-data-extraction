"""Подстановка измеренных цифр в report.md.

В report.md цифры, которые даёт только реальный прогон, стоят в блоках
    <!-- AUTO:имя -->  ⟪ЗАПОЛНИТЬ после прогона⟫  <!-- /AUTO:имя -->
Скрипт заменяет содержимое каждого блока таблицей/текстом из артефактов прогона
(clean/, logs/, mart/). Повторный запуск перезаписывает блоки — ручной текст вне
блоков не трогается. Если данные получены на локальной имитации (адреса 127.0.0.1),
в каждый блок добавляется явная пометка «(на локальной имитации)».

    python -m src.fill_report                       # report.md в корне лабораторной
    python -m src.fill_report --report /tmp/r.md    # другой файл (для тестов)
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from . import config, rawio

AUTO = re.compile(r"(<!-- AUTO:(?P<name>[a-z_]+) -->)(?P<body>.*?)(<!-- /AUTO:(?P=name) -->)", re.S)


def md_table(df: pd.DataFrame, index: bool = False) -> str:
    if df is None or df.empty:
        return "_нет данных_"
    if index:
        df = df.reset_index()
    cols = [str(c) for c in df.columns]

    def cell(v) -> str:
        if v is None or (not isinstance(v, (list, dict, str)) and pd.isna(v)):
            return "—"
        if isinstance(v, float):
            return f"{int(v)}" if v.is_integer() else f"{v:g}"   # Int64 с NULL после SQL читается как float
        return str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def _json(name: str):
    p = config.LOG_DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _csv(path: Path) -> pd.DataFrame | None:
    return pd.read_csv(path) if path.exists() else None


def is_simulation() -> bool:
    if not config.CLEAN_EVENTS.exists():
        return False
    urls = pd.read_parquet(config.CLEAN_EVENTS, columns=["source_url"])["source_url"]
    return bool(urls.str.contains(r"//(?:127\.0\.0\.1|localhost)", regex=True).any())


# ------------------------------------------------------------------ блоки
def b_counts() -> str:
    s = _json("clean_summary.json")
    if not s:
        return None
    ev = s.get("events_by_type", {})
    raw = s["raw"]
    share = raw["синтетических"] / max(1, raw["всего строк"] - raw["синтетических"]) * 100
    return (f"Сырой слой: **{raw['всего строк']}** строк, из них синтетически испорченных "
            f"**{raw['синтетических']}** ({share:.1f} % от числа реальных записей). "
            f"Чистый слой: **{s['clean']['events']}** событий (issues — {ev.get('issue', 0)}, "
            f"pull requests — {ev.get('pull_request', 0)}, релизы — {ev.get('release', 0)}) "
            f"по **{s['clean']['repositories']}** репозиториям; в rejects — **{s['rejects']}** записей.")


def b_collect() -> str:
    files = sorted(config.LOG_DIR.glob("collect_*.json"))
    if not files:
        return None
    # Сбор можно запускать частями (--no-html / --no-api): берём последний запуск с API и последний с HTML
    runs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    c_api = next((r for r in reversed(runs) if r.get("api")), {})
    c_html = next((r for r in reversed(runs) if r.get("html")), {})
    c = c_html or c_api
    api, html = c_api.get("api", {}), c_html.get("html", {})
    run_ids = " + ".join(dict.fromkeys(r["run_id"] for r in (c_api, c_html) if r))
    duration = round(sum(r.get("duration_s") or 0 for r in {id(x): x for x in (c_api, c_html) if x}.values()), 1)
    rows = [
        {"показатель": "запуск (run_id)", "значение": run_ids},
        {"показатель": "длительность сбора, с", "значение": duration},
        {"показатель": "API: запросов / байт", "значение": f"{api.get('requests')} / {api.get('bytes')}"},
        {"показатель": "API: коды ответов", "значение": json.dumps(api.get("status"), ensure_ascii=False)},
        {"показатель": "API: остаток лимита до / после",
         "значение": f"{c_api.get('api_plan', {}).get('rate_remaining_before')} / "
                     f"{api.get('rate_after', {}).get('X-RateLimit-Remaining')}"},
        {"показатель": "HTML: запросов / байт", "значение": f"{html.get('requests')} / {html.get('bytes')}"},
        {"показатель": "HTML: коды ответов", "значение": json.dumps(html.get("status"), ensure_ascii=False)},
        {"показатель": "robots.txt: статус / запрещённых запросов отправлено",
         "значение": f"{html.get('robots_status')} / {html.get('disallowed_requests')}"},
    ]
    if c.get("stopped"):
        rows.append({"показатель": "остановка", "значение": c["stopped"]})
    out = md_table(pd.DataFrame(rows))
    rob = _json(html.get("robots_file", "")) if html.get("robots_file") else None
    if rob:
        ctrl = [d for d in rob["decisions"] if d["purpose"].startswith("контроль")]
        used = [d for d in rob["decisions"] if not d["purpose"].startswith("контроль")][:2]
        df = pd.DataFrame([{"путь": d["path"], "назначение": d["purpose"],
                            "RFC 9309": "можно" if d["rfc9309"] else "нельзя",
                            "urllib.robotparser": {True: "можно", False: "нельзя", None: "—"}[d["urllib"]],
                            "итог": "можно" if d["allowed"] else "нельзя", "правило": d["reason"]}
                           for d in used + ctrl])
        out += "\n\nПроверка robots.txt (фрагмент `logs/" + html["robots_file"] + "`):\n\n" + md_table(df)
    return out


def b_raw() -> str:
    rows = []
    for p in sorted(config.RAW_DIR.glob("*.jsonl")):
        if p.name == config.RAW_MANIFEST.name:
            continue
        recs = rawio.read_jsonl(p)
        ents = pd.Series([r.get("_entity") for r in recs]).value_counts().to_dict()
        check = rawio.verify(p)
        rows.append({"файл": f"raw/{p.name}", "строк": len(recs), "размер, КБ": round(p.stat().st_size / 1024, 1),
                     "сущности": ", ".join(f"{k}: {v}" for k, v in ents.items()),
                     "дозаписей в MANIFEST": check.get("appends", "—"),
                     "неизменность": "подтверждена" if check.get("checked") else check.get("reason")})
    syn = rawio.read_jsonl(config.RAW_SYNTHETIC)
    out = md_table(pd.DataFrame(rows))
    if syn:
        kinds = pd.Series([r["_corruption"] for r in syn]).value_counts()
        out += "\n\nТипы синтетической порчи (`_corruption`): " + ", ".join(f"{k} — {v}" for k, v in kinds.items())
    return out


def b_formats() -> str:
    df = _csv(config.FORMAT_BENCH)
    meta = _json("format_benchmark_meta.json")
    if df is None:
        return None
    out = md_table(df.set_index("формат").T.rename_axis("показатель"), index=True)
    if meta:
        out += (f"\n\nНабор: {meta['rows']} строк (clean/dataset.parquet), время — медиана из {meta['repeats']} "
                f"повторов, pandas {meta['pandas']}. Parquet меньше CSV в **{meta['csv_to_parquet_size_ratio']}** раза.")
        trap = meta.get("csv_digits_only_batch")
        if trap:
            out += (f" Ловушка CSV: в партии из {trap['строк в партии']} строк, где все цвета меток — цифры, "
                    f"`{trap['было']}` после записи в CSV и чтения стало `{trap['стало после CSV']}` "
                    f"(тип {trap['тип']}); спасает только явный `dtype=str` → {trap['с dtype=str']}.")
    return out


def b_cleaning() -> str:
    df = _csv(config.CLEAN_LOG)
    return md_table(df) if df is not None else None


def b_rules() -> str:
    df = _csv(config.LOG_DIR / "rule_checks.csv")
    return md_table(df) if df is not None else None


def b_contract() -> str:
    df = _csv(config.LOG_DIR / "contract_probes.csv")
    return md_table(df) if df is not None else None


def b_rejects() -> str:
    q = _json("quality_latest.json")
    if not config.CLEAN_REJECTS.exists():
        return None
    rej = [json.loads(x) for x in config.CLEAN_REJECTS.read_text(encoding="utf-8").splitlines() if x.strip()]
    df = pd.DataFrame([{"этап": r["stage"], "поле/правило": r["field"], "сущность": r["entity"],
                        "синтетическая": r["synthetic"]} for r in rej])
    if df.empty:
        return "_отбракованных записей нет_"
    t = df.groupby(["этап", "поле/правило", "сущность"]).agg(записей=("синтетическая", "size"),
                                                           из_них_синтетических=("синтетическая", "sum"))
    out = md_table(t.reset_index().rename(columns={"из_них_синтетических": "из них синтетических"}))
    real = int((~df["синтетическая"]).sum())
    out += f"\n\nВсего в rejects: {len(df)}; из них реальных (не синтетических) записей: **{real}**."
    if q is not None:
        out += " Каждая запись хранит исходный payload, этап, поле и причину (`clean/rejects.jsonl`, таблица `rejects` в БД)."
    return out


def b_rebuild() -> str:
    r = _json("rebuild.json")
    if not r:
        return None
    fb, fa = r.get("fingerprint_before") or {}, r.get("fingerprint_after") or {}
    rows = [{"набор": k, "строк до": (fb.get(k) or {}).get("rows"), "SHA-256 до": (fb.get(k) or {}).get("sha256"),
             "строк после": (fa.get(k) or {}).get("rows"), "SHA-256 после": (fa.get(k) or {}).get("sha256")}
            for k in ("events", "repositories", "rejects")]
    return (md_table(pd.DataFrame(rows)) + f"\n\nПересборка без сети заняла {r['seconds']} с; результат идентичен: "
            f"**{'да' if r.get('identical') else 'нет'}**.")


def b_load() -> str:
    d = _json("load_demo.json")
    if not d:
        return None
    runs = pd.DataFrame(d["runs"]).drop(columns=["load_ts"]).set_index("прогон").T.rename_axis("показатель")
    out = md_table(runs, index=True)
    out += f"\n\nВ прогоне 3 изменены: репозиторий `{d['changed_keys']['repo_key']}` (звёзды +25) и событие " \
           f"`{d['changed_keys']['event_key']}` (open → closed, комментарий +1).\n\n"
    out += "История (SCD2) изменённых записей:\n\n" + md_table(pd.DataFrame(d["repo_history"])) + "\n\n" + \
           md_table(pd.DataFrame(d["event_history"]))
    b = d["asof_between"]
    out += (f"\n\nЗапрос «состояние на дату» для момента `{b['ts']}` (после прогона 2, до прогона 3) возвращает "
            f"{d['asof_full_between_rows']} репозиториев; для изменённого — "
            f"stars = {b['repo'][0]['stars'] if b['repo'] else '—'}, событие в состоянии "
            f"`{b['event'][0]['state'] if b['event'] else '—'}`; на текущий момент — stars = "
            f"{d['asof_now']['repo'][0]['stars'] if d['asof_now']['repo'] else '—'}, состояние "
            f"`{d['asof_now']['event'][0]['state'] if d['asof_now']['event'] else '—'}`.")
    out += "\n\nОтказ ограничений БД (простые INSERT заведомо плохих строк):\n\n" + md_table(pd.DataFrame(d["constraints"]))
    return out


def b_quality() -> str:
    df = _csv(config.LOG_DIR / "quality_latest.csv")
    q = _json("quality_latest.json")
    red = _json("red_gate_demo.json")
    if df is None:
        return None
    out = md_table(df)
    if q:
        out += f"\n\n**Вердикт: {q['verdict']}.**"
        det = q.get("optional_detail") or {}
        if det:
            out += " Полнота необязательных полей по отдельности: " + ", ".join(f"{k} — {v} %" for k, v in det.items()) + "."
    if red:
        out += (f"\n\nДемонстрация стоп-крана — сценарий «{red['scenario']}»: вердикт «{red['verdict']}», "
                f"`enforce_gate()` → `QualityGateError: {red['gate']}`")
    return out


def b_mart() -> str:
    m = _json("mart_summary.json")
    if not m:
        return None
    out = (f"Представления: " + ", ".join(f"`mart/views/{k}.parquet` ({v} строк)" for k, v in m["views"].items())
           + f". Партиции снапшотов: {', '.join('`' + p + '`' for p in m['partitions'])}; чтение одной партиции "
             f"`dt={m['snapshot_dt']}` с фильтром по ключу вернуло {m['read_one_partition_rows']} строк.\n\n")
    act = config.MART_DIR / "views" / "repo_activity.parquet"
    if act.exists():
        a = pd.read_parquet(act)[["repo_key", "issues", "pull_requests", "releases_seen", "pr_merge_rate_pct",
                                  "median_hours_to_close"]].head(6)
        out += "Фрагмент `repo_activity`:\n\n" + md_table(a) + "\n\n"
    top = config.MART_DIR / "views" / "top_repos.parquet"
    if top.exists():
        t = pd.read_parquet(top)[["repo_key", "stars", "used_by", "contributors", "open_items_per_1k_stars",
                                  "rank_by_stars", "rank_by_used_by"]].head(5)
        out += "Фрагмент `top_repos`:\n\n" + md_table(t) + "\n\n"
    out += "\n".join(f"![{c}](mart/charts/{c})" for c in m["charts"])
    return out


def b_duckdb() -> str:
    df = _csv(config.LOG_DIR / "duckdb_bench.csv")
    return md_table(df) if df is not None else None


def b_timings() -> str:
    t = _json("pipeline_timings.json")
    if not t:
        return None
    return md_table(pd.DataFrame([{"этап": k, "секунд": v} for k, v in t.items()]))


BLOCKS = {"counts": b_counts, "collect": b_collect, "raw": b_raw, "formats": b_formats, "cleaning": b_cleaning,
          "rules": b_rules, "contract": b_contract, "rejects": b_rejects, "rebuild": b_rebuild, "load": b_load,
          "quality": b_quality, "mart": b_mart, "duckdb": b_duckdb, "timings": b_timings}


def fill(text: str) -> tuple[str, list[str], list[str]]:
    sim = is_simulation()
    filled, missing = [], []

    def repl(m: re.Match) -> str:
        name = m.group("name")
        fn = BLOCKS.get(name)
        body = fn() if fn else None
        if body is None:
            missing.append(name)
            return m.group(0)
        filled.append(name)
        if sim:
            body = "> ⚠ **(на локальной имитации)** — цифры получены на синтетических фикстурах, не реальные.\n\n" + body
        return f"{m.group(1)}\n{body}\n{m.group(4)}"
    return AUTO.sub(repl, text), filled, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, default=config.PROJECT_ROOT / "report.md")
    args = ap.parse_args(argv)
    text = args.report.read_text(encoding="utf-8")
    new, filled, missing = fill(text)
    args.report.write_text(new, encoding="utf-8")
    print(f"report: {args.report} — заполнено блоков: {len(filled)} ({', '.join(filled)})")
    if missing:
        print("Нет данных для блоков:", ", ".join(missing))
    left = new.count("⟪ЗАПОЛНИТЬ")
    print(f"Осталось маркеров ⟪ЗАПОЛНИТЬ⟫: {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
