"""Хранилище: схема БД (db/schema.sql), идемпотентная загрузка UPSERT, история, отбраковка, слой mart.

UPSERT: INSERT ... ON CONFLICT(<бизнес-ключ>) DO UPDATE ... WHERE row_hash IS NOT excluded.row_hash —
повторный прогон не создаёт дублей, а updated_at меняется только у реально изменившихся записей.
Счётчики «вставлено / обновлено / без изменений» считаются по сравнению хэшей до загрузки.
Ограничения БД — второй рубеж после контракта: строка, нарушившая CHECK, уходит в rejects, а не роняет прогон.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from observability import RunContext
from settings import DATA_ROOT, DIRS, ROOT, iso, utcnow

REPO_COLS = ["repo_key", "repo_id", "full_name", "owner_login", "owner_type", "description", "homepage",
             "language", "license_spdx", "topics_json", "stars", "forks", "open_issues", "subscribers",
             "size_kb", "is_archived", "is_fork", "created_at", "pushed_at", "gh_updated_at", "default_branch",
             "etag", "row_hash", "source", "collected_at", "first_seen_at", "updated_at", "parser_version", "run_id"]
REPO_BUSINESS = ["full_name", "owner_type", "description", "homepage", "language", "license_spdx", "topics_json",
                 "stars", "forks", "open_issues", "subscribers", "size_kb", "is_archived", "is_fork",
                 "created_at", "pushed_at", "gh_updated_at", "default_branch", "repo_id"]
LISTING_COLS = ["topic", "repo_key", "full_name", "page", "rank_in_topic", "stars_html", "stars_html_approx",
                "language_html", "description_html", "tags_json", "html_updated_at", "row_hash", "source",
                "collected_at", "first_seen_at", "last_seen_at", "updated_at", "parser_version", "run_id"]
LISTING_BUSINESS = ["full_name", "page", "rank_in_topic", "stars_html", "stars_html_approx", "language_html",
                    "description_html", "tags_json", "html_updated_at"]


def connect(cfg: dict, db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DATA_ROOT / cfg["storage"]["db_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript((ROOT / cfg["storage"]["schema_path"]).read_text(encoding="utf-8"))
    conn.commit()
    return conn


def _hash(row: dict, fields: list[str]) -> str:
    return hashlib.sha1(json.dumps([row.get(f) for f in fields], ensure_ascii=False, default=str)
                        .encode("utf-8")).hexdigest()


def _ts(v: Any) -> str | None:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, str):
        return v
    return iso(pd.Timestamp(v).to_pydatetime())


def _nullable_int(v: Any) -> int | None:
    return None if v is None or pd.isna(v) else int(v)


def repo_rows(repos: pd.DataFrame, now: str) -> list[dict]:
    rows = []
    for r in repos.to_dict("records"):
        row = {
            "repo_key": r["repo_key"], "repo_id": int(r["repo_id"]), "full_name": r["full_name"],
            "owner_login": r["owner_login"], "owner_type": r.get("owner_type"),
            "description": r.get("description"), "homepage": r.get("homepage"), "language": r.get("language"),
            "license_spdx": r.get("license_spdx"),
            "topics_json": json.dumps(list(r.get("topics") if r.get("topics") is not None else []), ensure_ascii=False),
            "stars": int(r["stars"]), "forks": int(r["forks"]), "open_issues": int(r["open_issues"]),
            "subscribers": _nullable_int(r.get("subscribers")), "size_kb": int(r["size_kb"]),
            "is_archived": int(bool(r["is_archived"])), "is_fork": int(bool(r["is_fork"])),
            "created_at": _ts(r["created_at"]), "pushed_at": _ts(r.get("pushed_at")),
            "gh_updated_at": _ts(r["gh_updated_at"]), "default_branch": r.get("default_branch"),
            "etag": r.get("etag"), "source": r["source"], "collected_at": _ts(r["collected_at"]),
            "first_seen_at": _ts(r.get("first_seen_at")) or _ts(r["collected_at"]),
            "updated_at": now, "parser_version": r["parser_version"], "run_id": r["run_id"],
        }
        row["row_hash"] = _hash(row, REPO_BUSINESS)
        rows.append(row)
    return rows


def listing_rows(listings: pd.DataFrame, now: str) -> list[dict]:
    rows = []
    for r in listings.to_dict("records"):
        row = {
            "topic": r["topic"], "repo_key": r["repo_key"], "full_name": r["full_name"], "page": int(r["page"]),
            "rank_in_topic": int(r["rank_in_topic"]), "stars_html": _nullable_int(r.get("stars_html")),
            "stars_html_approx": int(bool(r["stars_html_approx"])), "language_html": r.get("language_html"),
            "description_html": r.get("description_html"),
            "tags_json": json.dumps(list(r.get("tags") if r.get("tags") is not None else []), ensure_ascii=False),
            "html_updated_at": _ts(r.get("html_updated_at")), "source": r["source"],
            "collected_at": _ts(r["collected_at"]), "first_seen_at": _ts(r["first_seen_at"]),
            "last_seen_at": _ts(r["last_seen_at"]), "updated_at": now,
            "parser_version": r["parser_version"], "run_id": r["run_id"],
        }
        row["row_hash"] = _hash(row, LISTING_BUSINESS)
        rows.append(row)
    return rows


def _upsert_sql(table: str, cols: list[str], key: list[str], keep_on_update: tuple[str, ...]) -> str:
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key and c not in keep_on_update)
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({', '.join(key)}) DO UPDATE SET {updates} "
            f"WHERE {table}.row_hash IS NOT excluded.row_hash")


def upsert(conn: sqlite3.Connection, table: str, cols: list[str], key: list[str], rows: list[dict],
           touch_cols: tuple[str, ...], ctx: RunContext) -> dict:
    """Идемпотентная загрузка. Возвращает {inserted, updated, unchanged, db_rejected}."""
    existing = {tuple(r[:-1]): r[-1] for r in
                conn.execute(f"SELECT {', '.join(key)}, row_hash FROM {table}").fetchall()}
    sql = _upsert_sql(table, cols, key, keep_on_update=("first_seen_at",))
    inserted = updated = unchanged = 0
    db_rejected: list[dict] = []
    touch = [r for r in rows if tuple(r[k] for k in key) in existing
             and existing[tuple(r[k] for k in key)] == r["row_hash"]]
    with conn:
        for row in rows:
            k = tuple(row[c] for c in key)
            try:
                conn.execute(sql, row)
            except sqlite3.IntegrityError as e:
                db_rejected.append({"source": table, "record_key": "|".join(map(str, k)), "field": "__db__",
                                    "reason": f"ограничение БД: {e}", "errors": [], "payload": row})
                continue
            if k not in existing:
                inserted += 1
            elif existing[k] != row["row_hash"]:
                updated += 1
            else:
                unchanged += 1
        # «прикосновение» без смены updated_at: когда видели / когда проверяли — не считается изменением
        if touch and touch_cols:
            set_clause = ", ".join(f"{c} = :{c}" for c in touch_cols)
            where = " AND ".join(f"{c} = :{c}" for c in key)
            conn.executemany(f"UPDATE {table} SET {set_clause} WHERE {where}",
                             [{c: r[c] for c in (*touch_cols, *key)} for r in touch])
    res = {"inserted": inserted, "updated": updated, "unchanged": unchanged, "db_rejected": len(db_rejected)}
    ctx.log("upsert", table=table, **res)
    return {**res, "_db_rejects": db_rejected}


def load_all(conn: sqlite3.Connection, clean: dict, ctx: RunContext, verified: dict[str, str]) -> dict:
    """Загрузка чистого слоя: repos, topic_listings, синонимы, снапшоты, отбраковка."""
    now = iso(utcnow())
    out: dict[str, Any] = {}
    db_rejects: list[dict] = []
    if not clean["repos"].empty:
        rows = repo_rows(clean["repos"], now)
        with conn:   # переименование/перенос: старая строка с тем же repo_id уступает место новому ключу
            cur = conn.executemany("DELETE FROM repos WHERE repo_id = :repo_id AND repo_key != :repo_key",
                                   [{"repo_id": x["repo_id"], "repo_key": x["repo_key"]} for x in rows])
            if cur.rowcount and cur.rowcount > 0:
                ctx.log("repos_renamed_moved", rows=cur.rowcount)
        r = upsert(conn, "repos", REPO_COLS, ["repo_key"], rows, ("collected_at", "etag", "run_id"), ctx)
        db_rejects += r.pop("_db_rejects")
        out["repos"] = r
    if not clean["listings"].empty:
        r = upsert(conn, "topic_listings", LISTING_COLS, ["topic", "repo_key"],
                   listing_rows(clean["listings"], now), ("last_seen_at", "collected_at", "run_id"), ctx)
        db_rejects += r.pop("_db_rejects")
        out["listings"] = r
    with conn:
        conn.executemany("INSERT INTO repo_aliases (alias_key, repo_key, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(alias_key) DO UPDATE SET repo_key = excluded.repo_key, updated_at = excluded.updated_at "
                         "WHERE repo_aliases.repo_key IS NOT excluded.repo_key",
                         [(a, k, now) for a, k in clean["aliases"].items()])
        # снапшоты: репозитории, проверенные в этом прогоне (200/304/search)
        if verified and not clean["repos"].empty:
            day = now[:10]
            aliases = clean["aliases"]
            by_key = {r["repo_key"]: r for r in clean["repos"].to_dict("records")}
            snaps = []
            for key, how in verified.items():
                rec = by_key.get(aliases.get(key, key))
                if rec is None:
                    continue
                snaps.append({"repo_key": rec["repo_key"], "snapshot_date": day, "stars": int(rec["stars"]),
                              "forks": int(rec["forks"]), "open_issues": int(rec["open_issues"]),
                              "subscribers": _nullable_int(rec.get("subscribers")), "size_kb": int(rec["size_kb"]),
                              "pushed_at": _ts(rec.get("pushed_at")), "verified_by": how, "run_id": ctx.run_id})
            conn.executemany("INSERT OR REPLACE INTO repo_snapshots VALUES (:repo_key, :snapshot_date, :stars, "
                             ":forks, :open_issues, :subscribers, :size_kb, :pushed_at, :verified_by, :run_id)", snaps)
            out["snapshots"] = len(snaps)
        all_rejects = clean["rejects"] + db_rejects
        conn.executemany("INSERT INTO rejects (run_id, source, record_key, field, reason, payload_json, seen_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [(ctx.run_id, r["source"], r.get("record_key"), r["field"], r["reason"],
                           json.dumps(r.get("payload"), ensure_ascii=False, default=str), now) for r in all_rejects])
    out["db_rejected"] = len(db_rejects)
    if db_rejects:
        with (DIRS["clean"] / "rejects.jsonl").open("a", encoding="utf-8") as f:
            for r in db_rejects:
                f.write(json.dumps({"run_id": ctx.run_id, **r}, ensure_ascii=False, default=str) + "\n")
    return out


# ------------------------------------------------------------------ журнал прогонов

def run_started(conn: sqlite3.Connection, ctx: RunContext, cfg: dict, resumed_from: str | None) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO pipeline_runs (run_id, started_at, status, resumed_from, html_base, "
                     "api_base) VALUES (?, ?, 'running', ?, ?, ?)",
                     (ctx.run_id, iso(ctx.started_at), resumed_from, cfg["sources"]["html"]["base_url"],
                      cfg["sources"]["api"]["base_url"]))


def run_finished(conn: sqlite3.Connection, ctx: RunContext, report: dict) -> None:
    rec, st = report["records"], report.get("storage", {}).get("repos", {})
    with conn:
        conn.execute(
            "UPDATE pipeline_runs SET finished_at=?, status=?, verdict=?, exit_code=?, html_cards=?, api_200=?, "
            "api_304=?, clean_repos=?, clean_listings=?, rejects=?, inserted=?, updated=?, unchanged=?, "
            "mart_built=?, report_path=? WHERE run_id=?",
            (report["finished_at"], report["status"], report["verdict"], report["exit_code"],
             rec.get("html_cards"), rec.get("api_200"), rec.get("api_304"), rec.get("clean_repos"),
             rec.get("clean_listings"), rec.get("rejects"), st.get("inserted"), st.get("updated"),
             st.get("unchanged"), int(bool(report.get("mart_built"))), report.get("report_path"), ctx.run_id))


def run_history(conn: sqlite3.Connection, exclude_run: str, limit: int = 10) -> list[dict]:
    rows = conn.execute("SELECT * FROM pipeline_runs WHERE status='finished' AND run_id != ? "
                        "ORDER BY started_at DESC LIMIT ?", (exclude_run, limit)).fetchall()
    return [dict(r) for r in rows]


def listings_from_db(conn: sqlite3.Connection, topics: list[str], max_age_days: int) -> list[dict]:
    """Деградация: если HTML-источник недоступен, ключи для обогащения берутся из уже известных карточек."""
    cutoff = iso(utcnow() - timedelta(days=max_age_days))
    q = (f"SELECT topic, repo_key, full_name, html_updated_at FROM topic_listings WHERE last_seen_at >= ? "
         f"AND topic IN ({', '.join('?' * len(topics))})")
    return [dict(r) for r in conn.execute(q, (cutoff, *topics)).fetchall()]


# ------------------------------------------------------------------ слой mart

def _spearman(a: pd.Series, b: pd.Series) -> float | None:
    m = a.notna() & b.notna()
    if m.sum() < 10:
        return None
    return round(float(a[m].rank().corr(b[m].rank())), 3)


def build_mart(conn: sqlite3.Connection, cfg: dict, ctx: RunContext) -> dict[str, pd.DataFrame]:
    """Представления и агрегаты под исследовательский вопрос (соединение двух источников) -> mart/."""
    from clean import ACTIVITY_BANDS, SIZE_BANDS, _band

    cutoff = iso(utcnow() - timedelta(days=int(cfg["storage"]["listing_max_age_days"])))
    df = pd.read_sql_query("SELECT * FROM v_repo_topic WHERE last_seen_at >= ?", conn, params=(cutoff,))
    marts: dict[str, pd.DataFrame] = {}
    if df.empty:
        ctx.log("mart_empty", level="WARNING")
        return marts
    for c in ("created_at", "pushed_at", "gh_updated_at", "api_collected_at", "last_seen_at"):
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    ref = df["api_collected_at"]
    df["age_days"] = (ref - df["created_at"]).dt.total_seconds() / 86400
    df["days_since_push"] = (ref - df["pushed_at"]).dt.total_seconds() / 86400
    df["size_mb"] = df["size_kb"] / 1024
    df["stars_per_year"] = df["stars"] / (df["age_days"].clip(lower=30) / 365.25)
    df["issues_per_100_stars"] = 100 * df["open_issues"] / df["stars"].clip(lower=1)
    df["is_python"] = df["language"].fillna("").str.lower().eq("python")
    df["active_90d"] = df["days_since_push"] <= 90
    df["size_band"] = df["size_kb"].map(lambda v: _band(v, SIZE_BANDS))
    df["activity_band"] = df["days_since_push"].map(lambda v: _band(v, ACTIVITY_BANDS))
    # Обезличивание аналитического слоя: вместо owner/repo (логин владельца может указывать на человека) —
    # стабильный хэш; связь с исходной записью остаётся только в raw/clean/БД с ограниченным сроком хранения.
    df["repo_ref"] = df["repo_key"].map(lambda k: hashlib.sha256(k.encode("utf-8")).hexdigest()[:16])
    df = df.drop(columns=["repo_key", "full_name"])
    marts["repo_topic"] = df

    py = df[df["is_python"]]
    rows = []
    for topic, g in py.groupby("topic"):
        rows.append({
            "topic": topic, "repos": len(g),
            "stars_median": float(g["stars"].median()), "stars_p75": float(g["stars"].quantile(0.75)),
            "forks_median": float(g["forks"].median()), "size_mb_median": round(float(g["size_mb"].median()), 2),
            "stars_per_year_median": round(float(g["stars_per_year"].median()), 1),
            "active_90d_pct": round(100 * float(g["active_90d"].mean()), 1),
            "archived_pct": round(100 * float(g["is_archived"].mean()), 1),
            "days_since_push_median": round(float(g["days_since_push"].median()), 1),
            "issues_per_100_stars_median": round(float(g["issues_per_100_stars"].median()), 2),
            "spearman_size_stars": _spearman(g["size_kb"], g["stars"]),
            "spearman_size_days_since_push": _spearman(g["size_kb"], g["days_since_push"]),
        })
    marts["topic_summary"] = pd.DataFrame(rows).sort_values("stars_median", ascending=False)

    marts["size_band_topic"] = (py.groupby(["topic", "size_band"], observed=True)
                                .agg(repos=("repo_ref", "count"), stars_median=("stars", "median"),
                                     active_90d_pct=("active_90d", lambda s: round(100 * s.mean(), 1)))
                                .reset_index())
    marts["activity_band_topic"] = (py.groupby(["topic", "activity_band"], observed=True)
                                    .agg(repos=("repo_ref", "count"), stars_median=("stars", "median"),
                                         size_mb_median=("size_mb", "median"))
                                    .reset_index())
    snaps = pd.read_sql_query(
        "SELECT s.snapshot_date, l.topic, COUNT(*) AS repos, SUM(s.stars) AS stars_total "
        "FROM repo_snapshots s JOIN topic_listings l ON l.repo_key = s.repo_key "
        "GROUP BY s.snapshot_date, l.topic ORDER BY s.snapshot_date", conn)
    marts["stars_by_day_topic"] = snaps

    DIRS["mart"].mkdir(parents=True, exist_ok=True)
    for name, frame in marts.items():
        frame.to_parquet(DIRS["mart"] / f"{name}.parquet", index=False)
        if name != "repo_topic":
            frame.to_csv(DIRS["mart"] / f"{name}.csv", index=False)
    ctx.log("mart_built", **{k: len(v) for k, v in marts.items()})
    return marts
