"""Очистка raw → clean: шесть шагов в фиксированном порядке + контракт pydantic.

Порядок (обязателен по заданию):
    1. нормализация текста     — NFKC, \\xa0 и zero-width, схлопывание пробелов, обрезка
    2. бизнес-ключ             — repo_key «owner/name», event_key «repo#номер» / «repo@тег»;
                                 URL очищаются от параметров трекинга (ключ НЕ строится по URL)
    3. типизация               — числа из текста («54.3k» → 54300), nullable Int64, boolean,
                                 даты → datetime UTC (наивные не угадываются)
    4. пропуски                — обязательные → отбраковка с причиной, необязательные → NULL
    5. дубликаты               — полные, по ключу (оставляется самая свежая), почти-дубли;
                                 слияние API и HTML карточек репозитория по ключу
    6. бизнес-правила          — ≥ 5 правил предметной области, нарушения → отбраковка
затем контракт (src/contract.py) — второй рубеж, невалидное → rejects с полем и причиной.

Каждый шаг пишет строку протокола: строк до / после, изменено, удалено, в rejects, примечание.
Функция build() детерминирована: не использует текущее время и сеть, поэтому
пересборка из raw всегда даёт тот же результат (см. src/rebuild.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from . import config, rawio
from .contract import CLOCK_SKEW, Event, Repository, first_error
from .parsers import (canonical_url, issue_number_from_url, normalize_text, parse_compact_number,
                      repo_slug, split_language)

MARKERS = ("_synthetic", "_corruption")


# ================================================================== протокол и отбраковка
@dataclass
class Protocol:
    rows: list[dict] = field(default_factory=list)

    def add(self, step: str, entity: str, before: int, after: int, changed: int = 0,
            rejected: int = 0, note: str = "") -> None:
        self.rows.append({"шаг": step, "сущность": entity, "строк до": before, "строк после": after,
                          "изменено": changed, "удалено": before - after, "в rejects": rejected,
                          "примечание": note})

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


@dataclass
class Rejects:
    items: list[dict] = field(default_factory=list)

    def add(self, entity: str, key, stage: str, field_name: str | None, reason: str, raw: dict | None) -> None:
        raw = raw or {}
        self.items.append({
            "entity": entity, "business_key": None if _isna(key) else key, "stage": stage,
            "field": field_name, "reason": reason, "source": raw.get("_source"),
            "source_url": raw.get("_source_url"), "collected_at": raw.get("_collected_at"),
            "synthetic": bool(raw.get("_synthetic", False)), "corruption": raw.get("_corruption"),
            "raw_file": raw.get("_file"), "raw_line": raw.get("_line"),
            "payload": {k: v for k, v in raw.items() if k not in ("_file", "_line")},
        })

    def count(self, stage_prefix: str, entity: str | None = None) -> int:
        return sum(1 for r in self.items if r["stage"].startswith(stage_prefix)
                   and (entity is None or r["entity"] == entity))


def _isna(v) -> bool:
    if v is None:
        return True
    if isinstance(v, (list, dict, tuple, set)):
        return False
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _none(v):
    """NaN/NA/NaT → None, Timestamp → datetime, numpy-скаляры → Python."""
    if _isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if hasattr(v, "item") and not isinstance(v, (list, dict, str)):
        return v.item()
    return v


# ================================================================== чтение raw
RAW_FILES = (("api", config.RAW_API), ("html", config.RAW_HTML), ("synthetic", config.RAW_SYNTHETIC))


def read_raw(include_synthetic: bool = True) -> tuple[list[dict], list[dict]]:
    """Все строки сырого слоя (+ номер файла/строки), нечитаемые строки — отдельно."""
    records, broken = [], []
    for name, path in RAW_FILES:
        if name == "synthetic" and not include_synthetic:
            continue
        for line_no, rec, line in rawio.iter_jsonl(path):
            if rec is None:
                broken.append({"_file": path.name, "_line": line_no, "_text": line[:500]})
                continue
            rec["_file"], rec["_line"] = path.name, line_no
            records.append(rec)
    return records, broken


def _raw_hash(rec: dict, business_only: bool = False) -> str:
    skip = set(MARKERS) | {"_file", "_line"}
    if business_only:   # содержимое + адрес, без времени сбора и служебных отметок
        payload = {k: v for k, v in rec.items() if not k.startswith("_") or k == "_source_url"}
    else:
        payload = {k: v for k, v in rec.items() if k not in skip}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _frame(records: list[dict], raws: list[dict] | None = None) -> pd.DataFrame:
    """Рабочая таблица шага: поля записи + исходная запись (_raw) для отбраковки."""
    raws = raws if raws is not None else records
    keys: list[str] = []
    for r in records:
        for k in r:
            if k not in keys:
                keys.append(k)
    cols = {"_raw": pd.Series(raws, dtype=object)}
    for k in keys:
        cols[k] = pd.Series([r.get(k) for r in records], dtype=object)
    cols["_raw_hash"] = pd.Series([_raw_hash(r) for r in raws], dtype=object)
    cols["_raw_business_hash"] = pd.Series([_raw_hash(r, business_only=True) for r in raws], dtype=object)
    cols["_type_errors"] = pd.Series([dict() for _ in raws], dtype=object)
    return pd.DataFrame(cols)


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df.columns else pd.Series([None] * len(df), index=df.index, dtype=object)


# ================================================================== типизация (шаг 3)
_ISO_TZ = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})$")
_ISO_NAIVE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?$")


def parse_utc(v) -> tuple[pd.Timestamp | None, str | None]:
    """ISO 8601 со смещением → Timestamp UTC. Наивные даты НЕ считаются UTC — это ошибка."""
    if _isna(v):
        return None, None
    s = str(v).strip()
    if not s:
        return None, None
    if _ISO_NAIVE.match(s):
        return None, f"дата без часового пояса: {s!r}"
    if not _ISO_TZ.match(s):
        return None, f"не ISO 8601: {s!r}"
    try:
        ts = pd.Timestamp(s)
    except (ValueError, OverflowError):
        return None, f"некорректная дата: {s!r}"
    return ts.tz_convert("UTC"), None


def _dates(df: pd.DataFrame, src: str) -> tuple[pd.Series, int, int]:
    values, bad, naive = [], 0, 0
    for i, v in zip(df.index, _col(df, src)):
        ts, err = parse_utc(v)
        if err:
            df.at[i, "_type_errors"][src] = err
            bad += 1
            naive += "без часового пояса" in err
        values.append(ts if ts is not None else pd.NaT)
    s = pd.to_datetime(pd.Series(values, index=df.index, dtype=object), utc=True)
    return s, bad, naive


def _ints(df: pd.DataFrame, src: str) -> tuple[pd.Series, int]:
    out, bad = [], 0
    for i, v in zip(df.index, _col(df, src)):
        if _isna(v):
            out.append(None)
        elif isinstance(v, bool):
            out.append(None)
            df.at[i, "_type_errors"][src] = f"ожидалось целое, получено {v!r}"
            bad += 1
        elif isinstance(v, int):
            out.append(v)
        else:
            try:
                f = float(str(v).replace(",", "").strip())
                if not f.is_integer():
                    raise ValueError
                out.append(int(f))
            except ValueError:
                out.append(None)
                df.at[i, "_type_errors"][src] = f"не целое число: {v!r}"
                bad += 1
    return pd.Series(pd.array(out, dtype="Int64"), index=df.index), bad


def _bools(series: pd.Series) -> pd.Series:
    vals = [None if _isna(v) else bool(v) for v in series]
    return pd.Series(pd.array(vals, dtype="boolean"), index=series.index)


def _compact(df: pd.DataFrame, exact_col: str | None, short_col: str) -> tuple[list, list, list, int]:
    """Число со страницы: точное из title (если есть), иначе компактное «54.3k»."""
    values, lower, exact, bad = [], [], [], 0
    for i in df.index:
        ex_raw = df.at[i, exact_col] if exact_col and exact_col in df.columns else None
        sh_raw = df.at[i, short_col] if short_col in df.columns else None
        num, lb = parse_compact_number(ex_raw)
        is_exact = num is not None
        if num is None:
            num, lb = parse_compact_number(sh_raw)
        if num is None and not (_isna(sh_raw) and _isna(ex_raw)):
            df.at[i, "_type_errors"][short_col] = f"число со страницы не распознано: {sh_raw!r}"
            bad += 1
        values.append(num)
        lower.append(lb if num is not None else None)
        exact.append(is_exact if num is not None else None)
    return values, lower, exact, bad


def compact_half_unit(raw) -> float:
    """Погрешность округления компактной записи: «55.1k» → 50, «1.6m» → 50 000, «131k» → 500."""
    t = (normalize_text(str(raw)) or "").lower().replace(",", "") if not _isna(raw) else ""
    m = re.match(r"^\d+(?:\.(\d+))?([km]?)", t)
    if not m:
        return 0.0
    decimals = len(m.group(1) or "")
    mult = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    return 0.5 * mult / (10 ** decimals) if mult > 1 else 0.0


# ================================================================== шаги
TEXT_COLS = {
    "repos_api": ["full_name", "name", "owner_login", "description", "homepage", "html_url", "language",
                  "license_spdx", "default_branch", "repo_slug_requested", "_source_url"],
    "repos_html": ["repo_nwo_raw", "repository_id_raw", "about_raw", "homepage_raw", "license_raw",
                   "stars_raw", "stars_title_raw", "forks_raw", "forks_title_raw", "watchers_raw",
                   "releases_count_raw", "latest_release_tag_raw", "latest_release_date_raw", "used_by_raw",
                   "used_by_title_raw", "contributors_raw", "contributors_title_raw", "repo_slug_requested",
                   "_source_url", "_final_url"],
    "events": ["title", "state", "state_reason", "author_association", "html_url", "repository_url",
               "repo_slug_requested", "tag_raw", "tag_href_raw", "commit_sha_raw", "_source_url"],
}
LIST_COLS = {"repos_api": ["topics"], "repos_html": ["topics_raw", "languages_raw"],
             "events": ["labels_raw"]}


def step1_text(frames: dict, proto: Protocol) -> None:
    for name, df in frames.items():
        if df.empty:
            proto.add("1. нормализация текста", name, 0, 0)
            continue
        changed_rows = pd.Series(False, index=df.index)
        per_col = {}
        for col in TEXT_COLS[name]:
            if col not in df.columns:
                continue
            new = df[col].map(normalize_text)
            diff = [not (_isna(a) and _isna(b)) and a != b for a, b in zip(df[col], new)]
            per_col[col] = sum(diff)
            changed_rows |= pd.Series(diff, index=df.index)
            df[col] = new.astype(object)
        for col in LIST_COLS[name]:
            if col not in df.columns:
                continue
            new = df[col].map(lambda xs: [normalize_text(x) for x in xs if normalize_text(x)]
                              if isinstance(xs, list) else [])
            diff = [isinstance(a, list) and a != b for a, b in zip(df[col], new)]
            per_col[col] = sum(diff)
            changed_rows |= pd.Series(diff, index=df.index)
            df[col] = new
        if name == "events" and "labels" in df.columns:   # метки API — список словарей
            def norm_labels(xs):
                return [{"name": normalize_text(x.get("name")), "color": normalize_text(x.get("color"))}
                        for x in xs if isinstance(x, dict) and normalize_text(x.get("name"))] if isinstance(xs, list) else []
            new = df["labels"].map(norm_labels)
            diff = [isinstance(a, list) and a != b for a, b in zip(df["labels"], new)]
            per_col["labels"] = sum(diff)
            changed_rows |= pd.Series(diff, index=df.index)
            df["labels"] = new
        detail = ", ".join(f"{k}: {v}" for k, v in per_col.items() if v)
        proto.add("1. нормализация текста", name, len(df), len(df), int(changed_rows.sum()),
                  note=f"исправлено значений — {detail or 'нет'}")


def step2_keys(frames: dict, proto: Protocol) -> None:
    # --- карточки репозиториев из API
    df = frames["repos_api"]
    if not df.empty:
        keys = [repo_slug(a) or repo_slug(b) or repo_slug(c) for a, b, c in
                zip(_col(df, "full_name"), _col(df, "html_url"), _col(df, "repo_slug_requested"))]
        df["repo_key"] = pd.Series(keys, index=df.index, dtype=object)
        df["source_url_c"] = _col(df, "_source_url").map(canonical_url)
        df["owner"] = df["repo_key"].map(lambda k: k.split("/")[0] if k else None)
        df["name_c"] = df["repo_key"].map(lambda k: k.split("/")[1] if k else None)
        proto.add("2. бизнес-ключ", "repos_api", len(df), len(df),
                  note=f"repo_key построен: {df['repo_key'].notna().sum()}, не построен: {df['repo_key'].isna().sum()}")
    # --- страницы репозиториев (HTML)
    df = frames["repos_html"]
    if not df.empty:
        src = _col(df, "_source_url")
        keys = [repo_slug(a) or repo_slug(f) or repo_slug(b) for a, f, b in
                zip(_col(df, "repo_nwo_raw"), _col(df, "_final_url"), _col(df, "repo_slug_requested"))]
        df["repo_key"] = pd.Series(keys, index=df.index, dtype=object)
        df["source_url_c"] = src.map(canonical_url)
        tracked = sum(1 for a, b in zip(src, df["source_url_c"]) if a and b and "?" in a and b != a)
        proto.add("2. бизнес-ключ", "repos_html", len(df), len(df), changed=tracked,
                  note=f"repo_key построен: {df['repo_key'].notna().sum()}; URL очищено от трекинга: {tracked}")
    # --- события
    df = frames["events"]
    if not df.empty:
        rk, nums, tags, ek, urls = [], [], [], [], []
        tracked = 0
        for i in df.index:
            entity = df.at[i, "_entity"]
            if entity == "issue":
                key = repo_slug(df.at[i, "repository_url"]) or repo_slug(df.at[i, "repo_slug_requested"])
                num = df.at[i, "number"]
                num = num if isinstance(num, int) and not isinstance(num, bool) else issue_number_from_url(df.at[i, "html_url"])
                raw_url = df.at[i, "html_url"]
                url = canonical_url(raw_url)
                tag = None
                event_key = f"{key}#{num}" if key and num else None
            else:   # release
                href = df.at[i, "tag_href_raw"] or ""
                key = repo_slug(df.at[i, "repo_slug_requested"]) or repo_slug(config.CANONICAL_WEB + href)
                tag = df.at[i, "tag_raw"] or (href.split("/releases/tag/", 1)[1] if "/releases/tag/" in href else None)
                num = None
                raw_url = df.at[i, "_source_url"]
                url = canonical_url(config.CANONICAL_WEB + href) if href else None
                event_key = f"{key}@{tag}" if key and tag else None
            if raw_url and "?" in str(raw_url) and canonical_url(raw_url) != normalize_text(raw_url):
                tracked += 1
            rk.append(key)
            nums.append(num)
            tags.append(tag)
            ek.append(event_key)
            urls.append(url)
        df["repo_key"] = pd.Series(rk, index=df.index, dtype=object)
        df["number_i"] = pd.Series(nums, index=df.index, dtype=object)
        df["tag"] = pd.Series(tags, index=df.index, dtype=object)
        df["event_key"] = pd.Series(ek, index=df.index, dtype=object)
        df["url"] = pd.Series(urls, index=df.index, dtype=object)
        df["source_url_c"] = _col(df, "_source_url").map(canonical_url)
        proto.add("2. бизнес-ключ", "events", len(df), len(df), changed=tracked,
                  note=f"event_key построен: {df['event_key'].notna().sum()}, не построен: "
                       f"{df['event_key'].isna().sum()}; URL с трекингом нормализовано: {tracked}")


def step3_types(frames: dict, proto: Protocol) -> None:
    # --- API: репозитории
    df = frames["repos_api"]
    if not df.empty:
        notes, bad_total = [], 0
        for src, dst in (("id", "github_id"), ("stargazers_count", "stars"), ("forks_count", "forks"),
                         ("subscribers_count", "watchers"), ("open_issues_count", "open_issues_prs"),
                         ("size", "size_kb")):
            df[dst], bad = _ints(df, src)
            bad_total += bad
        for src, dst in (("created_at", "created_at_t"), ("updated_at", "gh_updated_at"),
                         ("pushed_at", "pushed_at_t"), ("_collected_at", "collected_at")):
            df[dst], bad, _ = _dates(df, src)
            bad_total += bad
        df["archived_b"] = _bools(_col(df, "archived"))
        notes.append(f"целые: 6 полей → Int64; даты → UTC; ошибок приведения: {bad_total}")
        proto.add("3. типизация", "repos_api", len(df), len(df), changed=bad_total, note="; ".join(notes))
    # --- HTML: страницы репозиториев (числа из текста)
    df = frames["repos_html"]
    if not df.empty:
        bad_total = 0
        for exact, short, dst in (("stars_title_raw", "stars_raw", "stars_html"),
                                  ("forks_title_raw", "forks_raw", "forks_html"),
                                  (None, "watchers_raw", "watchers_html"),
                                  ("releases_count_title_raw", "releases_count_raw", "releases_count"),
                                  ("used_by_title_raw", "used_by_raw", "used_by"),
                                  ("contributors_title_raw", "contributors_raw", "contributors")):
            vals, lower, is_exact, bad = _compact(df, exact, short)
            df[dst] = pd.Series(pd.array(vals, dtype="Int64"), index=df.index)
            if dst == "contributors":
                df["contributors_is_lower_bound"] = pd.Series(pd.array(lower, dtype="boolean"), index=df.index)
            if dst == "stars_html":
                df["stars_html_exact"] = pd.Series(pd.array(is_exact, dtype="boolean"), index=df.index)
            bad_total += bad
        df["html_repo_id"], bad = _ints(df, "repository_id_raw")
        bad_total += bad
        df["latest_release_at"], bad, _ = _dates(df, "latest_release_date_raw")
        bad_total += bad
        df["collected_at"], bad, _ = _dates(df, "_collected_at")
        bad_total += bad
        langs = [[f"{n} {p}" for n, p in (split_language(x) for x in xs) if n is not None]
                 if isinstance(xs, list) else [] for xs in _col(df, "languages_raw")]
        df["languages"] = pd.Series(langs, index=df.index, dtype=object)
        first = [split_language(xs[0]) if xs else (None, None) for xs in langs]
        df["primary_language_html"] = pd.Series([f[0] for f in first], index=df.index, dtype=object)
        df["primary_language_pct"] = pd.Series([f[1] for f in first], index=df.index, dtype="Float64")
        proto.add("3. типизация", "repos_html", len(df), len(df), changed=bad_total,
                  note=f"«54.3k»/«53,912» → Int64 (точное значение из title, если есть); "
                       f"не распознано значений: {bad_total}")
    # --- события
    df = frames["events"]
    if not df.empty:
        is_rel = df["_entity"] == "release"
        df["github_id"], bad1 = _ints(df, "id")
        df["number_t"], bad2 = _ints(df, "number_i")
        df["comments_t"], bad3 = _ints(df, "comments")
        df["reactions"], bad4 = _ints(df, "reactions_total")
        df["created_at_t"], bc, nc = _dates(df, "created_at")
        df["gh_updated_at"], bu, nu = _dates(df, "updated_at")
        df["closed_at_t"], bcl, ncl = _dates(df, "closed_at")
        df["merged_at"], bm, nm = _dates(df, "pr_merged_at")
        df["collected_at"], bk, nk = _dates(df, "_collected_at")
        is_pr = _bools(_col(df, "is_pull_request")).fillna(False)
        etype = ["release" if r else ("pull_request" if p else "issue") for r, p in zip(is_rel, is_pr)]
        df["event_type"] = pd.Series(etype, index=df.index, dtype=object)
        labels_raw = _col(df, "labels_raw")
        state = [("prerelease" if isinstance(lr, list) and "Pre-release" in lr else "released") if r
                 else (s.lower() if isinstance(s, str) else None)
                 for r, s, lr in zip(is_rel, _col(df, "state"), labels_raw)]
        df["state_t"] = pd.Series(state, index=df.index, dtype=object)
        df["labels_t"] = pd.Series([[x["name"] for x in xs] if isinstance(xs, list) else []
                                    for xs in _col(df, "labels")], index=df.index, dtype=object)
        df["label_color"] = pd.Series([xs[0]["color"].lower() if isinstance(xs, list) and xs and xs[0].get("color")
                                       else None
                                       for xs in _col(df, "labels")], index=df.index, dtype=object)
        df["author_is_bot"] = _bools(_col(df, "user_is_bot"))
        df["is_draft"] = _bools(_col(df, "draft"))
        df["commit_sha"] = _col(df, "commit_sha_raw").map(lambda s: s.lower() if isinstance(s, str) else None)
        bad_dates = bc + bu + bcl + bm + bk
        naive = nc + nu + ncl + nm + nk
        bad_num = bad1 + bad2 + bad3 + bad4
        proto.add("3. типизация", "events", len(df), len(df), changed=bad_dates + bad_num,
                  note=f"даты → datetime UTC; не разобрано дат: {bad_dates} (из них наивных: {naive}); "
                       f"целые → Int64, ошибок: {bad_num}; PR из /issues промаркированы: {int(is_pr.sum())}")


REQUIRED = {
    "repos_api": ["repo_key", "github_id", "created_at_t", "stars", "forks", "open_issues_prs", "collected_at",
                  "source_url_c"],
    "repos_html": ["repo_key", "collected_at", "source_url_c"],
    "events": ["event_key", "repo_key", "event_type", "title", "state_t", "created_at_t", "collected_at",
               "source_url_c", "url"],
}
OPTIONAL = {
    "repos_api": ["description", "homepage", "language", "license_spdx", "watchers", "pushed_at_t"],
    "repos_html": ["releases_count", "latest_release_tag_raw", "used_by", "contributors", "primary_language_pct",
                   "stars_html"],
    "events": ["labels_t", "label_color", "comments_t", "author_association", "gh_updated_at", "commit_sha"],
}
SRC_FIELD = {"created_at_t": "created_at", "state_t": "state", "source_url_c": "_source_url",
             "collected_at": "_collected_at", "github_id": "id", "stars": "stargazers_count",
             "forks": "forks_count", "open_issues_prs": "open_issues_count"}
ENTITY = {"repos_api": "repository", "repos_html": "repo_page", "events": "event"}


def step4_missing(frames: dict, proto: Protocol, rejects: Rejects) -> dict:
    completeness = {}
    for name, df in list(frames.items()):
        if df.empty:
            proto.add("4. пропуски", name, 0, 0)
            continue
        before = len(df)
        # записи-ошибки сбора (HTTP ≠ 200): в них нет данных вообще
        err = df["_status"].map(lambda s: s != 200) | _col(df, "_error").map(lambda e: not _isna(e))
        drop = pd.Series(False, index=df.index)
        for i in df.index[err]:
            rejects.add(ENTITY[name], None, "4. пропуски", "_status",
                        f"нет данных: HTTP {df.at[i, '_status']}", df.at[i, "_raw"])
            drop[i] = True
        for i in df.index[~err]:
            missing = [c for c in REQUIRED[name]
                       if _isna(df.at[i, c]) or (isinstance(df.at[i, c], str) and not df.at[i, c])]
            if missing:
                col = missing[0]
                src = SRC_FIELD.get(col, col)
                detail = df.at[i, "_type_errors"].get(src)
                reason = f"пропуск обязательного поля {src}" + (f" ({detail})" if detail else "")
                key = df.at[i, "event_key"] if "event_key" in df.columns else df.at[i, "repo_key"]
                rejects.add(ENTITY[name], key, "4. пропуски", src, reason, df.at[i, "_raw"])
                drop[i] = True
        frames[name] = df[~drop].copy()
        kept = frames[name]
        completeness[name] = {c: round(float(kept[c].map(lambda v: not _isna(v) and v != []).mean() * 100), 1)
                              for c in OPTIONAL[name] if c in kept.columns} if len(kept) else {}
        opt = ", ".join(f"{c} {v}%" for c, v in completeness[name].items())
        proto.add("4. пропуски", name, before, len(kept), rejected=int(drop.sum()),
                  note=f"обязательные → rejects; необязательные остаются NULL (полнота: {opt})")
    return completeness


def _business_sig(df: pd.DataFrame, cols: list[str]) -> list[str]:
    return [repr(tuple(_none(df.at[i, c]) for c in cols if c in df.columns)) for i in df.index]


EVENT_SIG = ["event_key", "title", "state_t", "state_reason", "closed_at_t", "merged_at", "comments_t",
             "labels_t", "gh_updated_at", "created_at_t", "commit_sha", "author_association"]
REPO_API_SIG = ["repo_key", "description", "stars", "forks", "watchers", "open_issues_prs", "archived_b",
                "pushed_at_t", "gh_updated_at"]
REPO_HTML_SIG = ["repo_key", "about_raw", "stars_html", "forks_html", "releases_count", "used_by",
                 "contributors", "latest_release_tag_raw", "languages"]


def _dedup(df: pd.DataFrame, key: str, order: list[str], sig_cols: list[str]) -> tuple[pd.DataFrame, dict]:
    stats = {"full": 0, "near": 0, "repeat": 0, "stale": 0}
    if df.empty:
        return df, stats
    full = df.duplicated(subset=["_raw_hash"], keep="first")      # побайтно одинаковые записи
    stats["full"] = int(full.sum())
    df = df[~full].copy()
    df["_order"] = range(len(df))
    df["_sig"] = _business_sig(df, sig_cols)
    df = df.sort_values(order + ["_order"], kind="mergesort", na_position="first")
    last = ~df.duplicated(subset=[key], keep="last")              # самая свежая версия ключа
    winners = df[last].set_index(key)
    for i in df.index[~last]:
        w = winners.loc[df.at[i, key]]
        if df.at[i, "_sig"] != w["_sig"]:
            stats["stale"] += 1           # старое состояние объекта
        elif df.at[i, "_raw_business_hash"] != w["_raw_business_hash"]:
            stats["near"] += 1            # совпали только после нормализации (пробелы, трекинг, регистр)
        else:
            stats["repeat"] += 1          # то же значение, повторное наблюдение
    return df[last].drop(columns=["_order", "_sig"]).copy(), stats


def step5_duplicates(frames: dict, proto: Protocol) -> pd.DataFrame:
    notes = {}
    for name, key, order, sig in (("repos_api", "repo_key", ["collected_at"], REPO_API_SIG),
                                  ("repos_html", "repo_key", ["collected_at"], REPO_HTML_SIG),
                                  ("events", "event_key", ["gh_updated_at", "collected_at"], EVENT_SIG)):
        before = len(frames[name])
        frames[name], st = _dedup(frames[name], key, order, sig)
        extra = ""
        if name == "events" and len(frames[name]):
            ev = frames[name]
            probe = ev.assign(_t=ev["title"].str.casefold(), _d=ev["created_at_t"].dt.floor("min"))
            similar = probe.duplicated(subset=["repo_key", "event_type", "_t", "_d"], keep=False).sum()
            extra = (f"; похожих событий (тот же заголовок и минута создания, разные номера): {int(similar)} — "
                     f"не удалялись, это разные объекты GitHub")
        notes[name] = st
        proto.add("5. дубликаты", name, before, len(frames[name]), changed=st["near"],
                  note=f"полных дублей: {st['full']}; почти-дублей (совпали после шагов 1–2): {st['near']}; "
                       f"повторных наблюдений: {st['repeat']}; устаревших версий: {st['stale']}; "
                       f"оставлена самая свежая запись{extra}")

    # --- слияние двух источников одной сущности «репозиторий» по ключу
    api, page = frames["repos_api"], frames["repos_html"]
    html_cols = ["repo_key", "stars_html", "stars_html_exact", "stars_raw", "forks_html", "watchers_html",
                 "releases_count", "latest_release_tag_raw", "latest_release_at", "used_by", "contributors",
                 "contributors_is_lower_bound", "primary_language_html", "primary_language_pct", "languages",
                 "html_repo_id", "about_raw", "homepage_raw", "collected_at", "source_url_c", "_raw"]
    page = page[[c for c in html_cols if c in page.columns]].rename(
        columns={"collected_at": "html_collected_at", "source_url_c": "html_source_url", "_raw": "_raw_html"})
    if "repo_key" not in api.columns:
        api = pd.DataFrame({"repo_key": pd.Series([], dtype=object)})
    if "repo_key" not in page.columns:
        page = pd.DataFrame({"repo_key": pd.Series([], dtype=object)})
    merged = api.merge(page, on="repo_key", how="outer", indicator=True)
    if "_merge" in merged.columns:
        both = int((merged["_merge"] == "both").sum())
        api_only = int((merged["_merge"] == "left_only").sum())
        html_only = int((merged["_merge"] == "right_only").sum())
    else:
        both = api_only = html_only = 0
    proto.add("5. дубликаты", "repositories (API ⨝ HTML)", len(api) + len(page), len(merged), changed=both,
              note=f"карточки API и HTML одного repo_key объединены в одну запись: {both}; "
                   f"только API: {api_only}; только HTML: {html_only} (проверяются правилом R-01)")
    return merged


def _rule_table(rules: dict[str, pd.Series], entity: str) -> list[dict]:
    return [{"сущность": entity, "правило": name, "нарушений": int((~ok).sum()),
             "доля, %": round(float((~ok).mean() * 100), 2) if len(ok) else 0.0} for name, ok in rules.items()]


def step6_rules(repos: pd.DataFrame, events: pd.DataFrame, proto: Protocol, rejects: Rejects
                ) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    table: list[dict] = []
    # ------------------------------------------------ репозитории
    before = len(repos)
    if before:
        has_api = repos["github_id"].notna() if "github_id" in repos.columns else pd.Series(False, index=repos.index)
        api_part = has_api
        nonneg = (repos[["stars", "forks", "open_issues_prs"]].fillna(0) >= 0).all(axis=1) & \
                 (repos["watchers"].isna() | (repos["watchers"].fillna(0) >= 0))
        dates_ok = (repos["created_at_t"].isna() | repos["pushed_at_t"].isna() |
                    (repos["created_at_t"] <= repos["pushed_at_t"] + CLOCK_SKEW)) & \
                   (repos["created_at_t"].isna() | (repos["created_at_t"] <= repos["collected_at"] + CLOCK_SKEW))
        rules = {
            "R-01 репозиторий подтверждён API (есть карточка /repos)": api_part,
            "R-02 счётчики API ≥ 0 (stars, forks, watchers, open issues)": nonneg | ~api_part,
            "R-03 created_at ≤ pushed_at и ≤ момента сбора": dates_ok | ~api_part,
        }
        # правила согласованности источников: нарушение отбраковывает HTML-часть, а не репозиторий
        hid = repos["html_repo_id"] if "html_repo_id" in repos.columns else pd.Series(pd.NA, index=repos.index)
        id_ok = hid.isna() | repos["github_id"].isna() | (hid == repos["github_id"])
        tol = [max(0.02 * float(a), compact_half_unit(raw) + 100) if not _isna(a) else None
               for a, raw, exact in zip(repos["stars"], _col(repos, "stars_raw"), _col(repos, "stars_html_exact"))]
        stars_ok = pd.Series([_isna(h) or _isna(a) or abs(float(h) - float(a)) <= t
                              for h, a, t in zip(_col(repos, "stars_html"), repos["stars"], tol)], index=repos.index)
        html_nonneg = pd.Series(True, index=repos.index)
        for c in ("used_by", "contributors", "releases_count", "forks_html", "watchers_html"):
            if c in repos.columns:
                html_nonneg &= repos[c].isna() | (repos[c].fillna(0) >= 0)
        if "primary_language_pct" in repos.columns:
            html_nonneg &= repos["primary_language_pct"].isna() | repos["primary_language_pct"].between(0, 100)
        rules_html = {
            "R-04 id репозитория на странице = id в API": id_ok,
            "R-05 звёзды на странице ≈ звёзды в API (±2 % или погрешность «k»)": stars_ok,
            "R-06 счётчики HTML ≥ 0, доля языка 0–100 %": html_nonneg,
        }
        table += _rule_table({**rules, **rules_html}, "repository")
        bad = ~pd.concat(rules.values(), axis=1).all(axis=1)
        for i in repos.index[bad]:
            failed = [n for n, ok in rules.items() if not ok[i]]
            raw = repos.at[i, "_raw"] if not _isna(repos.at[i, "_raw"]) else repos.at[i, "_raw_html"]
            rejects.add("repository" if failed[0] != list(rules)[0] else "repo_page", repos.at[i, "repo_key"],
                        "6. бизнес-правила", failed[0].split()[0], "нарушение правила: " + "; ".join(failed), raw)
        repos = repos[~bad].copy()
        html_bad = ~pd.concat([r.loc[repos.index] for r in rules_html.values()], axis=1).all(axis=1)
        html_fields = ["stars_html", "stars_html_exact", "forks_html", "watchers_html", "releases_count",
                       "latest_release_tag_raw", "latest_release_at", "used_by", "contributors",
                       "contributors_is_lower_bound", "primary_language_html", "primary_language_pct",
                       "html_repo_id", "html_collected_at", "html_source_url"]
        for i in repos.index[html_bad]:
            failed = [n for n, ok in rules_html.items() if not ok[i]]
            rejects.add("repo_page", repos.at[i, "repo_key"], "6. бизнес-правила", failed[0].split()[0],
                        "нарушение правила (HTML-часть отброшена, данные API сохранены): " + "; ".join(failed),
                        repos.at[i, "_raw_html"])
        if html_bad.any():
            for c in html_fields:
                if c in repos.columns:
                    repos[c] = repos[c].mask(html_bad)
            if "languages" in repos.columns:
                repos["languages"] = pd.Series([[] if b else v for v, b in zip(repos["languages"], html_bad)],
                                               index=repos.index, dtype=object)
        proto.add("6. бизнес-правила", "repositories", before, len(repos), changed=int(html_bad.sum()),
                  rejected=int(bad.sum() + html_bad.sum()),
                  note=f"правил: {len(rules) + len(rules_html)}; отбраковано репозиториев: {int(bad.sum())}; "
                       f"отброшено HTML-частей при расхождении с API: {int(html_bad.sum())}")
    # ------------------------------------------------ события
    before = len(events)
    if before:
        valid_repos = set(repos["repo_key"]) if len(repos) else set()
        is_rel = events["event_type"] == "release"
        created, closed = events["created_at_t"], events["closed_at_t"]
        merged, updated = events["merged_at"], events["gh_updated_at"]
        rules = {
            "E-01 событие не из будущего (created_at ≤ момента сбора)": created <= events["collected_at"] + CLOCK_SKEW,
            "E-02 closed_at ≥ created_at": closed.isna() | (closed >= created),
            "E-03 state=closed ⇔ задан closed_at (issue/PR)": is_rel | ((events["state_t"] == "closed") == closed.notna()),
            "E-04 merged_at только у PR, не раньше создания и только у закрытого":
                merged.isna() | ((events["event_type"] == "pull_request") & (merged >= created)
                                 & (events["state_t"] == "closed")),
            "E-05 число комментариев ≥ 0": events["comments_t"].isna() | (events["comments_t"].fillna(0) >= 0),
            "E-06 updated_at ≥ created_at": updated.isna() | (updated >= created),
            "E-07 релиз: есть тег и нет номера; issue/PR: есть номер": pd.Series(
                [(t is not None and _isna(n)) if r else (not _isna(n) and t is None)
                 for r, t, n in zip(is_rel, events["tag"], events["number_t"])], index=events.index),
            "E-08 репозиторий события есть в справочнике (ссылочная целостность)":
                events["repo_key"].isin(valid_repos),
        }
        table += _rule_table(rules, "event")
        bad = ~pd.concat(rules.values(), axis=1).all(axis=1)
        for i in events.index[bad]:
            failed = [n for n, ok in rules.items() if not ok[i]]
            rejects.add("event", events.at[i, "event_key"], "6. бизнес-правила", failed[0].split()[0],
                        "нарушение правила: " + "; ".join(failed), events.at[i, "_raw"])
        events = events[~bad].copy()
        proto.add("6. бизнес-правила", "events", before, len(events), rejected=int(bad.sum()),
                  note=f"правил: {len(rules)}; отбраковано событий: {int(bad.sum())}")
    return repos, events, table


# ================================================================== контракт
def _repo_payload(r: pd.Series) -> dict:
    has_html = not _isna(r.get("html_collected_at"))
    homepage = None if _isna(r.get("homepage")) else str(r.get("homepage"))
    if homepage and "://" not in homepage:            # в API бывает «scrapy.org» без схемы
        homepage = "https://" + homepage
    homepage = canonical_url(homepage) if homepage else None
    return {
        "repo_key": r["repo_key"], "github_id": _none(r["github_id"]), "owner": r["owner"], "name": r["name_c"],
        "description": _none(r.get("description")) or _none(r.get("about_raw")), "homepage": homepage,
        "primary_language": _none(r.get("language")), "license_spdx": _none(r.get("license_spdx")),
        "topics": r["topics"] if isinstance(r.get("topics"), list) else [],
        "stars": _none(r["stars"]), "forks": _none(r["forks"]), "watchers": _none(r.get("watchers")),
        "open_issues_prs": _none(r["open_issues_prs"]), "size_kb": _none(r.get("size_kb")),
        "default_branch": _none(r.get("default_branch")), "archived": bool(_none(r.get("archived_b")) or False),
        "created_at": _none(r["created_at_t"]), "gh_updated_at": _none(r.get("gh_updated_at")),
        "pushed_at": _none(r.get("pushed_at_t")),
        "releases_count": _none(r.get("releases_count")), "latest_release_tag": _none(r.get("latest_release_tag_raw")),
        "latest_release_at": _none(r.get("latest_release_at")), "used_by": _none(r.get("used_by")),
        "contributors": _none(r.get("contributors")),
        "contributors_is_lower_bound": _none(r.get("contributors_is_lower_bound")),
        "primary_language_pct": _none(r.get("primary_language_pct")),
        "languages": r["languages"] if isinstance(r.get("languages"), list) else [],
        "stars_html": _none(r.get("stars_html")),
        "html_collected_at": _none(r.get("html_collected_at")), "html_source_url": _none(r.get("html_source_url")),
        "source": "github_api+github_html" if has_html else "github_api",
        "source_url": r["source_url_c"],
        "parser_version": r["_parser_version"] + ("+" + config.HTML_PARSER_VERSION if has_html else ""),
        "collected_at": _none(r["collected_at"]),
    }


def _event_payload(r: pd.Series) -> dict:
    is_rel = r["event_type"] == "release"
    return {
        "event_key": r["event_key"], "repo_key": r["repo_key"], "event_type": r["event_type"],
        "github_id": _none(r.get("github_id")), "number": _none(r.get("number_t")), "tag": _none(r.get("tag")),
        "title": r["title"], "state": r["state_t"], "state_reason": _none(r.get("state_reason")),
        "is_draft": _none(r.get("is_draft")), "author_association": _none(r.get("author_association")),
        "author_is_bot": _none(r.get("author_is_bot")), "labels": r["labels_t"] if isinstance(r.get("labels_t"), list) else [],
        "label_color": _none(r.get("label_color")), "comments": _none(r.get("comments_t")),
        "reactions": _none(r.get("reactions")), "created_at": _none(r["created_at_t"]),
        "gh_updated_at": _none(r.get("gh_updated_at")), "closed_at": _none(r.get("closed_at_t")),
        "merged_at": _none(r.get("merged_at")), "commit_sha": _none(r.get("commit_sha")) if is_rel else None,
        "url": r["url"], "source": r["_source"], "source_url": r["source_url_c"],
        "parser_version": r["_parser_version"], "collected_at": _none(r["collected_at"]),
    }


def apply_contract(repos: pd.DataFrame, events: pd.DataFrame, proto: Protocol, rejects: Rejects
                   ) -> tuple[list[Repository], list[Event]]:
    good_repos: list[Repository] = []
    for _, r in repos.iterrows():
        try:
            good_repos.append(Repository(**_repo_payload(r)))
        except ValidationError as exc:
            fld, msg = first_error(exc)
            raw = r["_raw"] if not _isna(r.get("_raw")) else r.get("_raw_html")
            rejects.add("repository", r["repo_key"], "7. контракт", fld, msg, raw)
    proto.add("7. контракт pydantic", "repositories", len(repos), len(good_repos),
              rejected=len(repos) - len(good_repos), note="модель Repository")
    keys = {m.repo_key for m in good_repos}
    good_events: list[Event] = []
    for _, r in events.iterrows():
        if r["repo_key"] not in keys:
            rejects.add("event", r["event_key"], "7. контракт", "repo_key",
                        "репозиторий события не прошёл контракт (нарушение FK)", r["_raw"])
            continue
        try:
            good_events.append(Event(**_event_payload(r)))
        except ValidationError as exc:
            fld, msg = first_error(exc)
            rejects.add("event", r["event_key"], "7. контракт", fld, msg, r["_raw"])
    proto.add("7. контракт pydantic", "events", len(events), len(good_events),
              rejected=len(events) - len(good_events), note="модель Event")
    return good_repos, good_events


# ================================================================== сборка набора
REPO_INT = ["github_id", "stars", "forks", "watchers", "open_issues_prs", "size_kb", "releases_count", "used_by",
            "contributors", "stars_html"]
REPO_DT = ["created_at", "gh_updated_at", "pushed_at", "latest_release_at", "html_collected_at", "collected_at"]
EVENT_INT = ["github_id", "number", "comments", "reactions"]
EVENT_DT = ["created_at", "gh_updated_at", "closed_at", "merged_at", "collected_at"]


def to_frame(models: list, ints: list[str], dts: list[str], bools: list[str], key: str) -> pd.DataFrame:
    df = pd.DataFrame([m.model_dump() for m in models])
    if df.empty:
        return df
    for c in ints:
        df[c] = pd.array([None if _isna(v) else int(v) for v in df[c]], dtype="Int64")
    for c in dts:
        df[c] = pd.to_datetime(df[c], utc=True)
    for c in bools:
        df[c] = pd.array([None if _isna(v) else bool(v) for v in df[c]], dtype="boolean")
    return df.sort_values(key, kind="mergesort").reset_index(drop=True)


@dataclass
class CleanResult:
    events: pd.DataFrame
    repos: pd.DataFrame
    rejects: Rejects
    protocol: Protocol
    rules: list[dict]
    completeness: dict
    raw_counts: dict


def build(include_synthetic: bool = True) -> CleanResult:
    """raw → (repos, events, rejects, протокол). Без сети и без текущего времени."""
    proto, rejects = Protocol(), Rejects()
    records, broken = read_raw(include_synthetic)
    for b in broken:
        rejects.add("raw", None, "0. чтение raw", None, "нечитаемая строка JSON", b)
    by = {"repo": [], "issue": [], "repo_page": [], "release": []}
    for r in records:
        by.setdefault(r.get("_entity"), []).append(r)
    raw_counts = {"всего строк": len(records) + len(broken), "нечитаемых": len(broken),
                  "синтетических": sum(1 for r in records if r.get("_synthetic")),
                  **{f"_entity={k}": len(v) for k, v in by.items()}}
    # у релиза заголовок и дата лежат в title_raw / datetime_raw — приводим к общей схеме событий
    rel_view = [{**r, "title": r.get("title_raw"), "created_at": r.get("datetime_raw")} for r in by["release"]]
    frames = {"repos_api": _frame(by["repo"]), "repos_html": _frame(by["repo_page"]),
              "events": _frame(by["issue"] + rel_view, by["issue"] + by["release"])}
    for name, df in frames.items():
        proto.add("0. чтение raw", name, len(df), len(df),
                  note=f"из них синтетических: {int(_col(df, '_synthetic').map(lambda v: v is True).sum())}")

    step1_text(frames, proto)
    step2_keys(frames, proto)
    # различных бизнес-ключей в сыром слое — знаменатель метрики «валидность по объектам»
    ev, ra, rh = frames["events"], frames["repos_api"], frames["repos_html"]
    repo_keys = set(_col(ra, "repo_key").dropna()) | set(_col(rh, "repo_key").dropna())
    raw_counts["различных ключей"] = {
        "events": int(_col(ev, "event_key").nunique() + _col(ev, "event_key").isna().sum()),
        "repositories": len(repo_keys) + int(_col(ra, "repo_key").isna().sum() + _col(rh, "repo_key").isna().sum()),
    }
    step3_types(frames, proto)
    completeness = step4_missing(frames, proto, rejects)
    repos = step5_duplicates(frames, proto)
    repos, events, rule_table = step6_rules(repos, frames["events"], proto, rejects)
    good_repos, good_events = apply_contract(repos, events, proto, rejects)

    repos_df = to_frame(good_repos, REPO_INT, REPO_DT, ["archived", "contributors_is_lower_bound"], "repo_key")
    events_df = to_frame(good_events, EVENT_INT, EVENT_DT, ["is_draft", "author_is_bot"], "event_key")
    return CleanResult(events_df, repos_df, rejects, proto, rule_table, completeness, raw_counts)


def save(res: CleanResult) -> dict:
    """Запись чистого слоя: Parquet (zstd) + rejects.jsonl + протокол."""
    config.CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    res.events.to_parquet(config.CLEAN_EVENTS, index=False, compression="zstd")
    res.repos.to_parquet(config.CLEAN_REPOS, index=False, compression="zstd")
    with config.CLEAN_REJECTS.open("w", encoding="utf-8") as f:     # производный файл, пересобирается целиком
        for item in res.rejects.items:
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    res.protocol.frame().to_csv(config.CLEAN_LOG, index=False, encoding="utf-8")
    pd.DataFrame(res.rules).to_csv(config.LOG_DIR / "rule_checks.csv", index=False, encoding="utf-8")
    summary = {
        "clean_version": config.CLEAN_VERSION, "raw": res.raw_counts,
        "clean": {"events": len(res.events), "repositories": len(res.repos)},
        "rejects": len(res.rejects.items),
        "rejects_by_stage": pd.Series([r["stage"] for r in res.rejects.items]).value_counts().to_dict()
        if res.rejects.items else {},
        "rejects_synthetic": sum(1 for r in res.rejects.items if r["synthetic"]),
        "completeness_optional": res.completeness,
        "events_by_type": res.events["event_type"].value_counts().to_dict() if len(res.events) else {},
        "events_by_source": res.events["source"].value_counts().to_dict() if len(res.events) else {},
    }
    (config.LOG_DIR / "clean_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                                                   default=str), encoding="utf-8")
    return summary


def main() -> None:
    rawio.verify_all()
    res = build()
    summary = save(res)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_colwidth", 120)
    print(res.protocol.frame().to_string(index=False))
    print(f"\nЧистый слой: событий {summary['clean']['events']}, репозиториев {summary['clean']['repositories']}; "
          f"в rejects: {summary['rejects']}")


if __name__ == "__main__":
    main()
