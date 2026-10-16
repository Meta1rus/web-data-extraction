"""Загрузка чистого слоя в SQLite: UPSERT по бизнес-ключу + история SCD2 + rejects.

    python db/load.py                    # clean/ → db/warehouse.db (рабочая загрузка)
    python db/load.py --demo             # три прогона (идемпотентность) и проверка ограничений
                                         # на отдельной копии db/demo_idempotency.db

Идемпотентность обеспечивают:
* INSERT … ON CONFLICT(key) DO UPDATE … WHERE row_hash IS NOT excluded.row_hash —
  строка обновляется (и получает новый updated_at) только при реальном изменении;
* executemany — пакетная загрузка;
* SCD2: новая версия в *_history появляется только при смене row_hash;
* rejects: UNIQUE(entity, payload_hash, reason) + DO NOTHING.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from src import config  # noqa: E402

REPO_COLS = ["repo_key", "github_id", "owner", "name", "description", "homepage", "primary_language",
             "license_spdx", "topics", "stars", "forks", "watchers", "open_issues_prs", "size_kb",
             "default_branch", "archived", "created_at", "gh_updated_at", "pushed_at", "releases_count",
             "latest_release_tag", "latest_release_at", "used_by", "contributors", "contributors_is_lower_bound",
             "primary_language_pct", "languages", "stars_html", "html_collected_at", "html_source_url",
             "source", "source_url", "parser_version", "collected_at"]
EVENT_COLS = ["event_key", "repo_key", "event_type", "github_id", "number", "tag", "title", "state",
              "state_reason", "is_draft", "author_association", "author_is_bot", "labels", "label_color",
              "comments", "reactions", "created_at", "gh_updated_at", "closed_at", "merged_at", "commit_sha",
              "url", "source", "source_url", "parser_version", "collected_at"]
# что НЕ входит в отпечаток версии: время и адрес наблюдения, версия парсера
NOT_HASHED = {"collected_at", "html_collected_at", "source_url", "html_source_url", "parser_version"}

REPO_HIST = ["stars", "forks", "watchers", "open_issues_prs", "releases_count", "latest_release_tag", "used_by",
             "contributors", "archived", "description"]
EVENT_HIST = ["repo_key", "event_type", "state", "state_reason", "title", "labels", "comments", "closed_at",
              "merged_at", "gh_updated_at"]


def load_ts() -> str:
    """Момент загрузки, UTC, миллисекунды: 2026-10-15T06:00:00.123Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sql_value(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)) or (hasattr(v, "tolist") and not isinstance(v, (str, bytes))
                                        and getattr(v, "ndim", 0) == 1):
        return json.dumps(list(v.tolist() if hasattr(v, "tolist") else v), ensure_ascii=False)
    if isinstance(v, (pd.Timestamp, datetime)):
        if pd.isna(v):
            return None
        ts = pd.Timestamp(v)
        if ts.tzinfo is None:
            raise ValueError("наивная дата не может попасть в хранилище")
        return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        is_missing = bool(pd.isna(v))
    except (TypeError, ValueError):
        is_missing = False  # для списков/массивов pd.isna неоднозначна — это не пропуск
    if is_missing:
        return None
    if isinstance(v, bool) or type(v).__name__ in ("bool_", "BooleanDtype"):
        return int(bool(v))
    if hasattr(v, "item"):
        v = v.item()
        return int(v) if isinstance(v, bool) else v
    return v


def to_rows(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    rows = []
    for rec in df[cols].to_dict(orient="records"):
        row = {c: _sql_value(rec[c]) for c in cols}
        hashed = {c: row[c] for c in cols if c not in NOT_HASHED}
        row["row_hash"] = hashlib.sha256(json.dumps(hashed, sort_keys=True, ensure_ascii=False,
                                                    default=str).encode()).hexdigest()
        rows.append(row)
    return rows


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(config.DB_SCHEMA.read_text(encoding="utf-8"))
    return conn


def upsert_sql(table: str, cols: list[str], key: str) -> str:
    all_cols = cols + ["updated_at", "row_hash"]
    sets = ",\n    ".join(f"{c} = excluded.{c}" for c in all_cols if c != key)
    return (f"INSERT INTO {table} ({', '.join(all_cols)})\n"
            f"VALUES ({', '.join(':' + c for c in all_cols)})\n"
            f"ON CONFLICT({key}) DO UPDATE SET\n    {sets}\n"
            f"WHERE {table}.row_hash IS NOT excluded.row_hash")   # обновляем только реально изменённое


def _upsert(conn: sqlite3.Connection, table: str, cols: list[str], key: str, rows: list[dict], ts: str) -> dict:
    before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    changes0 = conn.total_changes
    conn.executemany(upsert_sql(table, cols, key), [{**r, "updated_at": ts} for r in rows])
    touched = conn.total_changes - changes0
    after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    inserted = after - before
    return {"in": len(rows), "inserted": inserted, "updated": touched - inserted,
            "unchanged": len(rows) - touched, "rows_after": after}


def _scd2(conn: sqlite3.Connection, hist: str, key: str, attrs: list[str], rows: list[dict], ts: str,
          load_id: str) -> int:
    """SCD Type 2: закрыть изменившиеся версии и открыть новые одним набором запросов."""
    conn.execute("DROP TABLE IF EXISTS temp.stg")
    conn.execute(f"CREATE TEMP TABLE stg ({key} TEXT PRIMARY KEY, row_hash TEXT, collected_at TEXT, "
                 + ", ".join(f"{a}" for a in attrs) + ")")
    cols = [key, "row_hash", "collected_at"] + attrs
    conn.executemany(f"INSERT INTO temp.stg ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                     [{c: r.get(c) for c in cols} for r in rows])
    n0 = conn.total_changes
    conn.execute(f"""
        UPDATE {hist} SET valid_to = :ts, is_current = 0
        WHERE is_current = 1
          AND EXISTS (SELECT 1 FROM temp.stg s WHERE s.{key} = {hist}.{key} AND s.row_hash <> {hist}.row_hash)""",
                 {"ts": ts})
    closed = conn.total_changes - n0
    n1 = conn.total_changes
    conn.execute(f"""
        INSERT INTO {hist} ({key}, valid_from, valid_to, is_current, {', '.join(attrs)}, collected_at, row_hash, load_id)
        SELECT s.{key}, :ts, NULL, 1, {', '.join('s.' + a for a in attrs)}, s.collected_at, s.row_hash, :load_id
        FROM temp.stg s
        WHERE NOT EXISTS (SELECT 1 FROM {hist} h WHERE h.{key} = s.{key} AND h.is_current = 1)""",
                 {"ts": ts, "load_id": load_id})
    opened = conn.total_changes - n1
    conn.execute("DROP TABLE temp.stg")
    return opened


def _load_rejects(conn: sqlite3.Connection, ts: str, load_id: str) -> int:
    if not config.CLEAN_REJECTS.exists():
        return 0
    rows = []
    with config.CLEAN_REJECTS.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            payload = json.dumps(r["payload"], ensure_ascii=False, sort_keys=True, default=str)
            rows.append({"entity": r["entity"], "business_key": r["business_key"], "stage": r["stage"],
                         "field": r["field"], "reason": r["reason"], "is_synthetic": int(bool(r["synthetic"])),
                         "payload": payload, "payload_hash": hashlib.sha256(payload.encode()).hexdigest(),
                         "source": r["source"], "source_url": r["source_url"], "collected_at": r["collected_at"],
                         "first_seen_at": ts, "load_id": load_id})
    n0 = conn.total_changes
    conn.executemany("""
        INSERT INTO rejects (entity, business_key, stage, field, reason, is_synthetic, payload, payload_hash,
                             source, source_url, collected_at, first_seen_at, load_id)
        VALUES (:entity, :business_key, :stage, :field, :reason, :is_synthetic, :payload, :payload_hash,
                :source, :source_url, :collected_at, :first_seen_at, :load_id)
        ON CONFLICT(entity, payload_hash, reason) DO NOTHING""", rows)
    return conn.total_changes - n0


def load(conn: sqlite3.Connection, repos: pd.DataFrame, events: pd.DataFrame, label: str = "pipeline",
         with_rejects: bool = True) -> dict:
    """Одна загрузка в одной транзакции. Возвращает счётчики для отчёта."""
    ts = load_ts()
    load_id = f"{label}-{ts}"
    repo_rows, event_rows = to_rows(repos, REPO_COLS), to_rows(events, EVENT_COLS)
    with conn:                                   # всё или ничего
        r = _upsert(conn, "repositories", REPO_COLS, "repo_key", repo_rows, ts)
        e = _upsert(conn, "events", EVENT_COLS, "event_key", event_rows, ts)
        h = _scd2(conn, "repositories_history", "repo_key", REPO_HIST, repo_rows, ts, load_id)
        h += _scd2(conn, "events_history", "event_key", EVENT_HIST, event_rows, ts, load_id)
        rej = _load_rejects(conn, ts, load_id) if with_rejects else 0
        conn.execute("""INSERT INTO load_runs VALUES (:load_id, :label, :ts, :fin, :ri, :rins, :rupd, :runch,
                        :ei, :eins, :eupd, :eunch, :hist, :rej)""",
                     {"load_id": load_id, "label": label, "ts": ts, "fin": load_ts(), "ri": r["in"],
                      "rins": r["inserted"], "rupd": r["updated"], "runch": r["unchanged"], "ei": e["in"],
                      "eins": e["inserted"], "eupd": e["updated"], "eunch": e["unchanged"], "hist": h, "rej": rej})
    return {"load_id": load_id, "load_ts": ts, "repositories": r, "events": e, "history_versions_added": h,
            "rejects_added": rej}


def read_clean() -> tuple[pd.DataFrame, pd.DataFrame]:
    return pd.read_parquet(config.CLEAN_REPOS), pd.read_parquet(config.CLEAN_EVENTS)


# ================================================================== демонстрация
def _changed_copy(repos: pd.DataFrame, events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Прогон 3: изменились ровно две записи — один репозиторий и одно событие."""
    repos, events = repos.copy(), events.copy()
    later = pd.Timestamp(datetime.now(timezone.utc)).floor("s")
    ri = repos.index[0]
    repos.loc[ri, "stars"] = int(repos.loc[ri, "stars"]) + 25
    repos.loc[ri, "collected_at"] = later
    open_issues = events[(events["event_type"] == "issue") & (events["state"] == "open")]
    ei = open_issues.index[0] if len(open_issues) else events.index[0]
    events.loc[ei, "state"] = "closed"
    events.loc[ei, "state_reason"] = "completed"
    events.loc[ei, "closed_at"] = later
    events.loc[ei, "gh_updated_at"] = later
    comments = events.loc[ei, "comments"]
    events.loc[ei, "comments"] = (0 if pd.isna(comments) else int(comments)) + 1
    events.loc[ei, "collected_at"] = later
    return repos, events, {"repo_key": repos.loc[ri, "repo_key"], "event_key": events.loc[ei, "event_key"]}


def asof(conn: sqlite3.Connection, ts: str, key: str | None = None) -> pd.DataFrame:
    """Состояние справочника репозиториев на произвольный момент (SCD2)."""
    sql = """SELECT repo_key, stars, forks, open_issues_prs, valid_from, valid_to
             FROM repositories_history
             WHERE valid_from <= :ts AND (valid_to IS NULL OR valid_to > :ts)""" + (
        " AND repo_key = :key" if key else "") + " ORDER BY repo_key"
    return pd.read_sql_query(sql, conn, params={"ts": ts, "key": key})


def event_asof(conn: sqlite3.Connection, ts: str, key: str) -> pd.DataFrame:
    return pd.read_sql_query("""SELECT event_key, state, closed_at, comments, valid_from, valid_to
                                FROM events_history WHERE event_key = :key
                                  AND valid_from <= :ts AND (valid_to IS NULL OR valid_to > :ts)""",
                             conn, params={"ts": ts, "key": key})


CONSTRAINT_TESTS = [
    ("CHECK: отрицательное число звёзд", "repositories", {"stars": -5}),
    ("CHECK: наивная дата без Z", "repositories", {"created_at": "2024-05-14 13:23:43"}),
    ("PRIMARY KEY: повтор repo_key", "repositories", {}),
    ("UNIQUE: чужой github_id у нового ключа", "repositories", {"repo_key": "probe/dup-id"}),
    ("CHECK: state=closed без closed_at", "events", {"state": "closed", "closed_at": None}),
    ("CHECK: цвет метки не hex", "events", {"label_color": "ZZZZZZ"}),
    ("FOREIGN KEY: событие несуществующего репозитория", "events",
     {"repo_key": "ghost-org/ghost-repo", "event_key": "ghost-org/ghost-repo#1", "number": 1, "github_id": None}),
    ("CHECK: event_key не соответствует repo_key#number", "events", {"event_key": "probe#0", "github_id": None}),
]


def constraints_demo(conn: sqlite3.Connection) -> pd.DataFrame:
    """Пробуем вставить заведомо плохие строки простым INSERT — база должна отказать."""
    rows = []
    base_repo = dict(pd.read_sql_query("SELECT * FROM repositories LIMIT 1", conn).iloc[0])
    base_event = dict(pd.read_sql_query(
        "SELECT * FROM events WHERE event_type = 'issue' AND state = 'open' LIMIT 1", conn).iloc[0])
    counts0 = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("repositories", "events")}
    for n, (name, table, patch) in enumerate(CONSTRAINT_TESTS, start=1):
        if table == "repositories":
            # свежий ключ, чтобы срабатывало именно проверяемое ограничение (кроме теста PK)
            fresh = ({"github_id": 900_000_000 + n} if "PRIMARY KEY" in name else
                     {"repo_key": f"probe/check-{n}", "owner": "probe", "name": f"check-{n}",
                      "github_id": 900_000_000 + n})
            if "UNIQUE" in name:
                fresh.pop("github_id")
            row = {**base_repo, **fresh, **patch}
        else:
            num = 9_000_000 + n
            fresh = {"event_key": f"{base_event['repo_key']}#{num}", "number": num, "github_id": None}
            row = {**base_event, **fresh, **patch}
        row = {k: (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)) for k, v in row.items()}
        cols = list(row)
        try:
            with conn:
                conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             [row[c] for c in cols])
            rows.append({"проверка": name, "таблица": table, "результат": "ВСТАВЛЕНО (плохо)", "ошибка СУБД": ""})
        except sqlite3.IntegrityError as exc:
            rows.append({"проверка": name, "таблица": table, "результат": "отклонено базой",
                         "ошибка СУБД": str(exc)[:110]})
    counts1 = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("repositories", "events")}
    assert counts0 == counts1, "ограничения пропустили строку"
    return pd.DataFrame(rows)


def demo(db_path: Path | None = None, pause: float = 1.2) -> dict:
    """Три прогона загрузки + история + отказ ограничений. Пишет logs/load_demo.json."""
    db_path = db_path or config.DEMO_DB
    if db_path.exists():
        db_path.unlink()                    # демонстрационная база всегда с нуля
    repos, events = read_clean()
    conn = connect(db_path)
    out: dict = {"db": db_path.name, "runs": []}

    def snapshot(tag: str, res: dict) -> dict:
        rcount = conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
        ecount = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        upd_r = conn.execute("SELECT COUNT(*) FROM repositories WHERE updated_at = ?", (res["load_ts"],)).fetchone()[0]
        upd_e = conn.execute("SELECT COUNT(*) FROM events WHERE updated_at = ?", (res["load_ts"],)).fetchone()[0]
        hist = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("repositories_history", "events_history"))
        return {"прогон": tag, "load_ts": res["load_ts"],
                "repos: передано": res["repositories"]["in"], "repos: вставлено": res["repositories"]["inserted"],
                "repos: обновлено": res["repositories"]["updated"],
                "repos: без изменений": res["repositories"]["unchanged"],
                "events: передано": res["events"]["in"], "events: вставлено": res["events"]["inserted"],
                "events: обновлено": res["events"]["updated"],
                "events: без изменений": res["events"]["unchanged"],
                "строк repositories": rcount, "строк events": ecount,
                "строк с updated_at этого прогона": upd_r + upd_e,
                "новых версий в истории": res["history_versions_added"], "строк истории всего": hist,
                "rejects добавлено": res["rejects_added"]}

    r1 = load(conn, repos, events, label="run1-initial")
    out["runs"].append(snapshot("1. исходный", r1))
    time.sleep(pause)
    r2 = load(conn, repos, events, label="run2-same")
    out["runs"].append(snapshot("2. те же данные", r2))
    time.sleep(pause)
    repos3, events3, changed = _changed_copy(repos, events)
    r3 = load(conn, repos3, events3, label="run3-two-changed")
    out["runs"].append(snapshot("3. изменены 2 записи", r3))
    out["changed_keys"] = changed

    # история: версии изменённых записей и запрос «состояние на дату»
    out["repo_history"] = pd.read_sql_query(
        "SELECT repo_key, stars, valid_from, valid_to, is_current FROM repositories_history "
        "WHERE repo_key = ? ORDER BY valid_from", conn, params=(changed["repo_key"],)).to_dict(orient="records")
    out["event_history"] = pd.read_sql_query(
        "SELECT event_key, state, closed_at, comments, valid_from, valid_to, is_current FROM events_history "
        "WHERE event_key = ? ORDER BY valid_from", conn, params=(changed["event_key"],)).to_dict(orient="records")
    between = r2["load_ts"]                      # момент между прогоном 1 и прогоном 3
    out["asof_between"] = {"ts": between,
                           "repo": asof(conn, between, changed["repo_key"]).to_dict(orient="records"),
                           "event": event_asof(conn, between, changed["event_key"]).to_dict(orient="records")}
    now = load_ts()
    out["asof_now"] = {"ts": now, "repo": asof(conn, now, changed["repo_key"]).to_dict(orient="records"),
                       "event": event_asof(conn, now, changed["event_key"]).to_dict(orient="records")}
    out["asof_full_between_rows"] = len(asof(conn, between))

    cons = constraints_demo(conn)
    out["constraints"] = cons.to_dict(orient="records")
    conn.close()
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    (config.LOG_DIR / "load_demo.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                                                   encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Загрузка clean → SQLite (UPSERT + SCD2)")
    ap.add_argument("--db", type=Path, default=config.WAREHOUSE_DB)
    ap.add_argument("--demo", action="store_true", help="три прогона и проверка ограничений на demo-базе")
    ap.add_argument("--label", default="pipeline")
    args = ap.parse_args(argv)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    if args.demo:
        out = demo()
        print(pd.DataFrame(out["runs"]).drop(columns=["load_ts"]).T.to_string())
        print("\nИзменённые ключи:", out["changed_keys"])
        print(pd.DataFrame(out["repo_history"]).to_string(index=False))
        print(pd.DataFrame(out["event_history"]).to_string(index=False))
        print("\nОграничения БД:")
        print(pd.DataFrame(out["constraints"]).to_string(index=False))
        return 0
    repos, events = read_clean()
    conn = connect(args.db)
    res = load(conn, repos, events, label=args.label)
    conn.close()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
