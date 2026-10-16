"""Очистка с протоколом: raw (JSON Lines) -> clean (Parquet) + rejects.jsonl.

Порядок шагов фиксирован (занятие 6) и каждый шаг пишет в протокол, сколько записей затронул:
  1) чтение сырого слоя (всех прогонов: raw только дописывается, из него всегда можно пересобрать clean);
  2) нормализация текста (NFKC, неразрывные и невидимые символы, схлопывание пробелов);
  3) бизнес-ключ: owner/repo в нижнем регистре (не URL и не номер страницы);
  4) приведение типов: числа -> int, даты -> UTC, справочные значения (лицензия SPDX, тип владельца);
  5) пропуски -> None (не пустая строка и не 0);
  6) дедупликация по ключу с сохранением самой свежей версии; для repos — ещё и по repo_id (переименования);
  7) контракт данных (pydantic) -> валидные / отбракованные с причиной;
  8) вычисляемые поля (возраст, дни с последнего push, звёзды в год, размер в МБ, диапазоны размера/активности).
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from collect_html import SOURCE as HTML_SOURCE, read_raw
from collect_api import SOURCE as API_SOURCE
from contract import RepoRecord, TopicListing, validate_records
from observability import RunContext
from settings import DIRS, parse_iso, write_json_atomic

_MIN_TS = datetime.min.replace(tzinfo=timezone.utc)
INVISIBLE = re.compile(r"[​-‏⁠﻿]")
SIZE_BANDS = [(0, 1024, "<1 МБ"), (1024, 10 * 1024, "1–10 МБ"), (10 * 1024, 100 * 1024, "10–100 МБ"),
              (100 * 1024, float("inf"), "≥100 МБ")]
ACTIVITY_BANDS = [(0, 30, "push ≤30 дн"), (31, 90, "31–90 дн"), (91, 365, "91–365 дн"),
                  (366, float("inf"), ">365 дн")]


def clean_text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = INVISIBLE.sub("", unicodedata.normalize("NFKC", v).replace("\xa0", " "))
    v = " ".join(v.split())
    return v or None


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


def listing_from_raw(env: dict) -> dict:
    p = env["payload"]
    full = clean_text(p.get("full_name")) or ""
    return {
        "topic": (env.get("topic") or "").strip().lower(),
        "repo_key": full.lower(),
        "full_name": full,
        "page": _int_or_none(env.get("page")),
        "rank_in_topic": _int_or_none(p.get("rank_in_topic")),
        "stars_html": _int_or_none(p.get("stars")),
        "stars_html_approx": bool(p.get("stars_approx")),
        "language_html": clean_text(p.get("language")),
        "description_html": clean_text(p.get("description")),
        "tags": [t for t in (clean_text(x) for x in (p.get("tags") or [])) if t],
        "html_updated_at": parse_iso(p.get("updated_raw")),
        "collected_at": parse_iso(env.get("_collected_at")),
        "source": HTML_SOURCE,
        "parser_version": env.get("_parser_version") or "unknown",
        "run_id": env.get("_run_id"),
    }


def repo_from_raw(env: dict) -> dict:
    p = env["payload"]
    owner = p.get("owner") or {}
    lic = p.get("license") or {}
    full = clean_text(p.get("full_name")) or ""
    owner_type = owner.get("type") if owner.get("type") in ("User", "Organization") else None
    return {
        "repo_key": full.lower(),
        "repo_id": _int_or_none(p.get("id")),
        "full_name": full,
        "owner_login": clean_text(owner.get("login")) or "",
        "owner_type": owner_type,
        "description": clean_text(p.get("description")),
        "homepage": clean_text(p.get("homepage")),
        "language": clean_text(p.get("language")),
        "license_spdx": clean_text(lic.get("spdx_id")),
        "topics": [t for t in (clean_text(x) for x in (p.get("topics") or [])) if t],
        "stars": _int_or_none(p.get("stargazers_count")),
        "forks": _int_or_none(p.get("forks_count")),
        "open_issues": _int_or_none(p.get("open_issues_count")),
        "subscribers": _int_or_none(p.get("subscribers_count")),
        "size_kb": _int_or_none(p.get("size")),
        "is_archived": bool(p.get("archived")),
        "is_fork": bool(p.get("fork")),
        "created_at": parse_iso(p.get("created_at")),
        "pushed_at": parse_iso(p.get("pushed_at")),
        "gh_updated_at": parse_iso(p.get("updated_at")),
        "default_branch": clean_text(p.get("default_branch")),
        "collected_at": parse_iso(env.get("_collected_at")),
        "source": "github_search_api" if env.get("_endpoint") == "search" else "github_rest_api",
        "parser_version": env.get("_parser_version") or "unknown",
        "run_id": env.get("_run_id"),
        "etag": env.get("_etag"),
        "_requested_key": (env.get("requested_key") or full).lower(),
    }


def _band(value: float | None, bands) -> str | None:
    if value is None or pd.isna(value):
        return None
    for lo, hi, name in bands:
        if lo <= value < hi or (hi == float("inf") and value >= lo):
            return name
    return None


def add_computed(repos: pd.DataFrame) -> pd.DataFrame:
    if repos.empty:
        return repos
    df = repos.copy()
    ref = df["collected_at"]
    df["age_days"] = (ref - df["created_at"]).dt.total_seconds() / 86400
    df["days_since_push"] = (ref - df["pushed_at"]).dt.total_seconds() / 86400
    df["stars_per_year"] = df["stars"] / (df["age_days"].clip(lower=30) / 365.25)
    df["size_mb"] = df["size_kb"] / 1024
    df["is_python"] = df["language"].fillna("").str.lower().eq("python")
    df["size_band"] = df["size_kb"].map(lambda v: _band(v, SIZE_BANDS))
    df["activity_band"] = df["days_since_push"].map(lambda v: _band(v, ACTIVITY_BANDS))
    df["pushed_before_created"] = (df["pushed_at"] < df["created_at"]).fillna(False)
    return df


def build_clean(cfg: dict, ctx: RunContext) -> dict:
    protocol: list[dict] = []

    def step(name: str, before: int, after: int, note: str = "") -> None:
        protocol.append({"шаг": name, "до": before, "после": after, "затронуто": before - after, "примечание": note})

    rejects: list[dict] = []

    # ---------- 1. сырой слой
    raw_l, raw_r = read_raw(HTML_SOURCE), read_raw(API_SOURCE)
    step("1. чтение raw: карточки тем", len(raw_l), len(raw_l), f"прогонов: {len({e['_run_id'] for e in raw_l})}")
    step("1. чтение raw: ответы API", len(raw_r), len(raw_r), f"прогонов: {len({e['_run_id'] for e in raw_r})}")

    # ---------- 2–5. нормализация, ключ, типы, пропуски
    def convert(envs: list[dict], fn, source: str) -> list[dict]:
        out = []
        for env in envs:
            try:
                out.append(fn(env))
            except (KeyError, ValueError, TypeError) as e:
                rejects.append({"source": source, "record_key": None, "field": "__convert__",
                                "reason": f"ошибка приведения типов: {type(e).__name__}: {e}",
                                "errors": [], "payload": env.get("payload")})
        return out

    listings = convert(raw_l, listing_from_raw, HTML_SOURCE)
    repos = convert(raw_r, repo_from_raw, API_SOURCE)
    changed_text = sum(1 for e in raw_l if (e["payload"].get("description") or None) !=
                       clean_text(e["payload"].get("description")))
    step("2–5. нормализация текста, ключ, типы (карточки)", len(raw_l), len(listings),
         f"описаний с исправленными пробелами/символами: {changed_text}")
    step("2–5. нормализация текста, ключ, типы (API)", len(raw_r), len(repos))

    # ---------- 6. дедупликация (самая свежая версия) + first/last seen
    seen: dict[tuple, dict] = {}
    first_last: dict[tuple, list] = {}
    for rec in sorted(listings, key=lambda r: r["collected_at"] or _MIN_TS):
        k = (rec["topic"], rec["repo_key"])
        fl = first_last.setdefault(k, [rec["collected_at"], rec["collected_at"]])
        fl[1] = rec["collected_at"]
        seen[k] = rec
    for k, rec in seen.items():
        rec["first_seen_at"], rec["last_seen_at"] = first_last[k]
    dedup_l = list(seen.values())
    step("6. дедупликация карточек по (topic, repo_key)", len(listings), len(dedup_l),
         "повторные наблюдения тех же карточек в разных прогонах; оставлена самая свежая")

    by_key: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    first_seen_r: dict[str, Any] = {}
    for rec in sorted(repos, key=lambda r: r["collected_at"] or _MIN_TS):
        first_seen_r.setdefault(rec["repo_key"], rec["collected_at"])
        if rec["_requested_key"] != rec["repo_key"]:
            aliases[rec["_requested_key"]] = rec["repo_key"]
        by_key[rec["repo_key"]] = rec
    by_id: dict[int, dict] = {}
    for rec in sorted(by_key.values(), key=lambda r: r["collected_at"]):
        if rec["repo_id"] is not None:
            old = by_id.get(rec["repo_id"])
            if old is not None and old["repo_key"] != rec["repo_key"]:
                aliases[old["repo_key"]] = rec["repo_key"]
            by_id[rec["repo_id"]] = rec
        else:
            by_id[-len(by_id) - 1] = rec                         # без id — отбракует контракт
    dedup_r = list(by_id.values())
    for rec in dedup_r:
        rec["first_seen_at"] = first_seen_r.get(rec["repo_key"], rec["collected_at"])
    step("6. дедупликация API по repo_key и repo_id", len(repos), len(dedup_r),
         f"переименований/синонимов ключа: {len(aliases)}")

    # ---------- 7. контракт
    l_fields = set(TopicListing.model_fields)
    r_fields = set(RepoRecord.model_fields)
    valid_l, rej_l = validate_records([{k: v for k, v in r.items() if k in l_fields} for r in dedup_l],
                                      TopicListing, HTML_SOURCE)
    valid_r, rej_r = validate_records([{k: v for k, v in r.items() if k in r_fields} for r in dedup_r],
                                      RepoRecord, API_SOURCE)
    seen_meta = {(r["topic"], r["repo_key"]): (r["first_seen_at"], r["last_seen_at"]) for r in dedup_l}
    for r in valid_l:
        r["first_seen_at"], r["last_seen_at"] = seen_meta[(r["topic"], r["repo_key"])]
    first_meta = {r["repo_key"]: r["first_seen_at"] for r in dedup_r}
    for r in valid_r:
        r["first_seen_at"] = first_meta.get(r["repo_key"], r["collected_at"])
    rejects += rej_l + rej_r
    step("7. контракт: карточки тем", len(dedup_l), len(valid_l), f"в rejects: {len(rej_l)}")
    step("7. контракт: репозитории API", len(dedup_r), len(valid_r), f"в rejects: {len(rej_r)}")

    # ---------- 8. вычисляемые поля и запись
    listings_df = pd.DataFrame(valid_l)
    repos_df = add_computed(pd.DataFrame(valid_r))
    step("8. вычисляемые поля", len(repos_df), len(repos_df),
         "age_days, days_since_push, stars_per_year, size_mb, size_band, activity_band, is_python")

    DIRS["clean"].mkdir(parents=True, exist_ok=True)
    if not listings_df.empty:
        listings_df.to_parquet(DIRS["clean"] / "topic_listings.parquet", index=False, compression="zstd")
    if not repos_df.empty:
        repos_df.to_parquet(DIRS["clean"] / "repos.parquet", index=False, compression="zstd")
    with (DIRS["clean"] / "rejects.jsonl").open("w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps({"run_id": ctx.run_id, **r}, ensure_ascii=False, default=str) + "\n")
    write_json_atomic(DIRS["clean"] / "cleaning_protocol.json", {"run_id": ctx.run_id, "steps": protocol})
    write_json_atomic(DIRS["clean"] / "repo_aliases.json", aliases)
    ctx.log("clean_built", listings=len(listings_df), repos=len(repos_df), rejects=len(rejects),
            aliases=len(aliases), raw_listings=len(raw_l), raw_repos=len(raw_r))
    return {"listings": listings_df, "repos": repos_df, "rejects": rejects, "protocol": protocol,
            "aliases": aliases, "raw_counts": {"listings": len(raw_l), "repos": len(raw_r)},
            "dedup_counts": {"listings": len(dedup_l), "repos": len(dedup_r)}}
