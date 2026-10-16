"""Синтетическая «порча» 5–10 % записей для демонстрации очистки и контракта.

Разрешено заданием (раздел 2). Правила, чтобы не исказить реальные данные:
* испорченные записи пишутся ОТДЕЛЬНЫМ файлом raw/synthetic_dirty.jsonl (только
  дозапись), реальные raw/github_*.jsonl не трогаются;
* каждая такая запись помечена `_synthetic: true` и `_corruption: <тип>`;
* копии реальных записей получают `_collected_at` на час раньше оригинала —
  при дедупликации побеждает настоящая (более свежая) запись;
* «битые» записи, которые должны отсеяться правилами, получают несуществующий
  ключ (номер + 10 000 000), поэтому не вытесняют реальные события.
Итог: в чистый слой не попадает ни одно синтетическое значение — порча либо
исправляется очисткой (и уходит как дубль), либо оказывается в rejects.

Запуск: python -m src.corrupt --share 0.07 --seed 5
Портится каждая новая партия сбора (_run_id) один раз; повторный запуск ничего не добавляет.
"""
from __future__ import annotations

import argparse
import copy
import random
from collections import Counter
from datetime import datetime, timedelta

from . import config, rawio

FAKE_OFFSET = 10_000_000

# тип → вес; «copy» — копия с тем же ключом, «fake» — новый (несуществующий) ключ
ISSUE_TYPES = {
    "nbsp_zero_width": 15, "tracking_url": 10, "case_slug": 5, "full_duplicate": 15,
    "missing_title": 8, "bad_date": 6, "naive_date": 4,
    "closed_without_closed_at": 7, "closed_before_created": 6, "negative_comments": 5,
    "created_in_future": 5, "merged_issue": 5, "orphan_repo": 4, "bad_label_color": 5,
}
RELEASE_TYPES = {"nbsp_zero_width": 30, "full_duplicate": 20, "tracking_url": 15,
                 "missing_datetime": 20, "orphan_repo": 15}
REPO_PAGE_TYPES = {"nbsp_zero_width": 40, "tracking_url": 30, "compact_number_cyrillic": 30}
REPO_API_TYPES = {"nbsp_zero_width": 40, "full_duplicate": 30, "missing_id": 30}
FAKE_TYPES = {"closed_without_closed_at", "closed_before_created", "negative_comments",
              "created_in_future", "merged_issue", "orphan_repo", "bad_label_color"}


def _shift(ts: str, **delta) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) + timedelta(**delta)
    return dt.isoformat(timespec="milliseconds")


def _iso_z(ts: str, **delta) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) + timedelta(**delta)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dirty_text(s: str | None) -> str:
    s = s or "untitled"
    words = s.split(" ")
    mid = max(1, len(words) // 2)
    return "  " + " ".join(words[:mid]) + "\xa0​\xa0" + " ".join(words[mid:]) + " \n"


def _fake_key(rec: dict) -> dict:
    """Новый несуществующий номер события, чтобы не вытеснять реальную запись."""
    rec["number"] = (rec.get("number") or 0) + FAKE_OFFSET
    if rec.get("id"):
        rec["id"] = rec["id"] + 50_000_000_000
    if rec.get("html_url"):
        base = rec["html_url"].rsplit("/", 1)[0]
        rec["html_url"] = f"{base}/{rec['number']}"
    return rec


def corrupt_issue(orig: dict, kind: str) -> dict:
    r = copy.deepcopy(orig)
    if kind not in FAKE_TYPES and kind != "full_duplicate":
        r["_collected_at"] = _shift(orig["_collected_at"], hours=-1)
    if kind == "full_duplicate":
        pass                                  # побайтная копия (сбой повторной попытки)
    elif kind == "nbsp_zero_width":
        r["title"] = _dirty_text(r.get("title"))
    elif kind == "tracking_url":
        r["html_url"] = (r.get("html_url") or "") + "?utm_source=newsletter&utm_medium=email"
    elif kind == "case_slug":
        r["repository_url"] = (r.get("repository_url") or "").upper().replace("HTTPS://API.GITHUB.COM", "https://api.github.com")
        r["repo_slug_requested"] = (r.get("repo_slug_requested") or "").upper()
    elif kind == "missing_title":
        r["title"] = " ​ "
    elif kind == "bad_date":
        r["created_at"] = "31.02.2024 25:61"
    elif kind == "naive_date":
        r["created_at"] = (r.get("created_at") or "2024-01-01T00:00:00Z").replace("T", " ").replace("Z", "")
    else:
        r = _fake_key(r)
        if kind == "closed_without_closed_at":
            r["state"], r["closed_at"] = "closed", None
        elif kind == "closed_before_created":
            r["state"] = "closed"
            r["closed_at"] = _iso_z(r["created_at"], days=-3)
        elif kind == "negative_comments":
            r["comments"] = -3
        elif kind == "created_in_future":
            r["created_at"] = _iso_z(r["_collected_at"], days=30)
            r["updated_at"] = r["created_at"]
        elif kind == "merged_issue":
            r["is_pull_request"] = False
            r["state"] = "closed"
            r["closed_at"] = r.get("closed_at") or _iso_z(r["created_at"], hours=5)
            r["pr_merged_at"] = r["closed_at"]
        elif kind == "orphan_repo":
            r["repository_url"] = "https://api.github.com/repos/ghost-org/ghost-repo"
            r["repo_slug_requested"] = "ghost-org/ghost-repo"
            r["html_url"] = f"https://github.com/ghost-org/ghost-repo/issues/{r['number']}"
        elif kind == "bad_label_color":
            r["labels"] = [{"name": "bug", "color": "#D73A4A"}]
    return r


def corrupt_release(orig: dict, kind: str) -> dict:
    r = copy.deepcopy(orig)
    if kind != "full_duplicate":
        r["_collected_at"] = _shift(orig["_collected_at"], hours=-1)
    if kind == "nbsp_zero_width":
        r["title_raw"] = _dirty_text(r.get("title_raw"))
    elif kind == "tracking_url":
        r["_source_url"] = (r.get("_source_url") or "") + "?utm_source=telegram"
    elif kind == "missing_datetime":
        r["datetime_raw"] = None
    elif kind == "orphan_repo":
        r["_collected_at"] = orig["_collected_at"]
        r["repo_slug_requested"] = "ghost-org/ghost-repo"
        r["tag_href_raw"] = r["tag_href_raw"].replace(r["tag_href_raw"].split("/releases/")[0], "/ghost-org/ghost-repo")
    return r


def corrupt_repo_page(orig: dict, kind: str) -> dict:
    r = copy.deepcopy(orig)
    r["_collected_at"] = _shift(orig["_collected_at"], hours=-1)
    if kind == "nbsp_zero_width":
        r["about_raw"] = _dirty_text(r.get("about_raw"))
    elif kind == "tracking_url":
        r["_source_url"] = (r.get("_source_url") or "") + "?utm_source=vk&utm_campaign=lab6"
    elif kind == "compact_number_cyrillic":
        r["stars_raw"] = (r.get("stars_raw") or "1k").replace("k", "к")      # кириллическая «к»
        r["stars_title_raw"] = None
    return r


def corrupt_repo_api(orig: dict, kind: str) -> dict:
    r = copy.deepcopy(orig)
    if kind != "full_duplicate":
        r["_collected_at"] = _shift(orig["_collected_at"], hours=-1)
    if kind == "nbsp_zero_width":
        r["description"] = _dirty_text(r.get("description"))
    elif kind == "missing_id":
        r["id"] = None
    return r


PLAN = {"issue": (ISSUE_TYPES, corrupt_issue), "release": (RELEASE_TYPES, corrupt_release),
        "repo_page": (REPO_PAGE_TYPES, corrupt_repo_page), "repo": (REPO_API_TYPES, corrupt_repo_api)}


def corrupt_batch(records: list[dict], run_id: str, share: float, seed: int) -> list[dict]:
    rng = random.Random(f"{seed}-{run_id}")
    out: list[dict] = []
    for entity, (types, fn) in PLAN.items():
        pool = [r for r in records if r.get("_entity") == entity and r.get("_status") == 200
                and "_error" not in r]
        if not pool:
            continue
        k = max(1, round(share * len(pool)))
        names, weights = zip(*types.items())
        for orig in rng.sample(pool, k=min(k, len(pool))):
            kind = rng.choices(names, weights)[0]
            rec = fn(orig, kind)
            rec["_synthetic"] = True
            rec["_corruption"] = kind
            out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Синтетическая порча 5–10 % записей (отдельный файл)")
    ap.add_argument("--share", type=float, default=0.07)
    ap.add_argument("--seed", type=int, default=5)
    args = ap.parse_args(argv)
    if not 0.05 <= args.share <= 0.10:
        ap.error("доля порчи по заданию — от 5 до 10 %")

    real = rawio.read_jsonl(config.RAW_API) + rawio.read_jsonl(config.RAW_HTML)
    done = {r.get("_run_id") for r in rawio.read_jsonl(config.RAW_SYNTHETIC)}
    runs = sorted({r.get("_run_id") for r in real} - done)
    if not runs:
        print("Новых партий нет — синтетика уже добавлена ранее, файл не меняется.")
        return 0
    total = Counter()
    for run_id in runs:
        batch = [r for r in real if r.get("_run_id") == run_id]
        dirty = corrupt_batch(batch, run_id, args.share, args.seed)
        rawio.append_records(config.RAW_SYNTHETIC, dirty, run_id=f"synthetic-{run_id}")
        total.update(d["_corruption"] for d in dirty)
        print(f"Партия {run_id}: реальных записей {len(batch)}, синтетических {len(dirty)} "
              f"({len(dirty) / max(1, len(batch)):.1%})")
    for kind, n in sorted(total.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {kind:28} {n}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
