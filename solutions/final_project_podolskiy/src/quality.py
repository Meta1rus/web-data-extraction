"""Метрики качества с порогами и итоговым вердиктом («светофор»).

Каждая метрика сравнивается с порогами green/yellow из config.yaml; значение хуже yellow — КРАСНЫЙ.
Итог: КРАСНЫЙ, если красная хотя бы одна метрика -> загрузка в mart не выполняется, код возврата 2.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from observability import RunContext
from settings import DIRS, utcnow, write_json_atomic

TITLES = {
    "required_completeness_pct": "полнота обязательных полей, %",
    "reject_share_pct": "доля отбракованных записей, %",
    "key_uniqueness_pct": "уникальность бизнес-ключа, %",
    "join_coverage_pct": "покрытие связи HTML -> API (обогащено), %",
    "stars_consistency_pct": "согласованность звёзд HTML и API, %",
    "api_freshness_p95_hours": "возраст данных API (p95), ч",
    "volume_vs_prev_pct": "объём чистого слоя к прошлому прогону, %",
    "python_share_pct": "доля репозиториев на Python, %",
    "clean_repos_count": "число обогащённых репозиториев для анализа",
}
REQUIRED_REPO = ["stars", "forks", "open_issues", "size_kb", "created_at", "gh_updated_at"]
REQUIRED_LISTING = ["stars_html", "html_updated_at", "language_html"]


def _status(value: float | None, rule: dict) -> str:
    if value is None:
        return "н/д"
    if rule["direction"] == "higher":
        return "green" if value >= rule["green"] else ("yellow" if value >= rule["yellow"] else "red")
    return "green" if value <= rule["green"] else ("yellow" if value <= rule["yellow"] else "red")


def compute_quality(clean: dict, cfg: dict, ctx: RunContext, prev_report: dict | None) -> dict:
    repos: pd.DataFrame = clean["repos"]
    listings: pd.DataFrame = clean["listings"]
    rejects = clean["rejects"]
    aliases = clean.get("aliases", {})
    rules = cfg["quality"]["metrics"]
    now = utcnow()

    fresh = listings
    if not listings.empty:
        cutoff = now - timedelta(days=int(cfg["storage"]["listing_max_age_days"]))
        fresh = listings[pd.to_datetime(listings["last_seen_at"], utc=True) >= cutoff]
    keys = set(fresh["repo_key"]) if not fresh.empty else set()
    resolved = {aliases.get(k, k) for k in keys}
    enriched = repos[repos["repo_key"].isin(resolved)] if not repos.empty else repos

    values: dict[str, float | None] = {}
    # 1. полнота обязательных полей (по ячейкам обеих сущностей)
    cells = filled = 0
    if not enriched.empty:
        cells += enriched[REQUIRED_REPO].size
        filled += int(enriched[REQUIRED_REPO].notna().sum().sum())
    if not fresh.empty:
        cells += fresh[REQUIRED_LISTING].size
        filled += int(fresh[REQUIRED_LISTING].notna().sum().sum())
    values["required_completeness_pct"] = round(100 * filled / cells, 2) if cells else None
    # 2. доля отбраковки от записей, дошедших до контракта
    total = clean["dedup_counts"]["listings"] + clean["dedup_counts"]["repos"]
    values["reject_share_pct"] = round(100 * len(rejects) / total, 2) if total else None
    # 3. уникальность ключа
    uniq = []
    if not repos.empty:
        uniq.append(repos["repo_key"].nunique() / len(repos))
    if not listings.empty:
        uniq.append((~listings.duplicated(["topic", "repo_key"])).mean())
    values["key_uniqueness_pct"] = round(100 * min(uniq), 3) if uniq else None
    # 4. покрытие связи
    values["join_coverage_pct"] = round(100 * len(set(enriched["repo_key"]) if not enriched.empty else set())
                                        / len(resolved), 2) if resolved else None
    # 5. согласованность звёзд HTML и API
    if not fresh.empty and not enriched.empty:
        tol = float(cfg["quality"]["stars_tolerance_pct"]) / 100
        m = fresh.assign(k=fresh["repo_key"].map(lambda k: aliases.get(k, k))).merge(
            enriched[["repo_key", "stars"]], left_on="k", right_on="repo_key", suffixes=("", "_api"))
        m = m[m["stars_html"].notna()]
        ok = (m["stars_html"] - m["stars"]).abs() <= (m["stars"] * tol).clip(lower=2)
        values["stars_consistency_pct"] = round(100 * float(ok.mean()), 2) if len(m) else None
    else:
        values["stars_consistency_pct"] = None
    # 6. свежесть данных API
    if not enriched.empty:
        age_h = (now - pd.to_datetime(enriched["collected_at"], utc=True)).dt.total_seconds() / 3600
        values["api_freshness_p95_hours"] = round(float(age_h.quantile(0.95)), 2)
    else:
        values["api_freshness_p95_hours"] = None
    # 7. объём к прошлому прогону
    prev_n = (prev_report or {}).get("records", {}).get("analysis_repos")
    values["volume_vs_prev_pct"] = round(100 * len(enriched) / prev_n, 1) if prev_n else None
    # 8. доля Python
    values["python_share_pct"] = (round(100 * float(enriched["language"].fillna("").str.lower().eq("python").mean()), 2)
                                  if not enriched.empty else None)
    # 9. объём для анализа
    values["clean_repos_count"] = float(len(enriched))

    metrics = []
    for name, rule in rules.items():
        v = values.get(name)
        metrics.append({"metric": name, "title": TITLES.get(name, name), "value": v,
                        "green": rule["green"], "yellow": rule["yellow"], "direction": rule["direction"],
                        "status": _status(v, rule)})
    statuses = [m["status"] for m in metrics]
    verdict = "red" if "red" in statuses else ("yellow" if "yellow" in statuses else "green")

    info = {}
    if not enriched.empty:
        info["pushed_before_created_pct"] = round(100 * float(enriched["pushed_before_created"].mean()), 2)
        info["archived_pct"] = round(100 * float(enriched["is_archived"].mean()), 2)
        info["forks_in_sample_pct"] = round(100 * float(enriched["is_fork"].mean()), 2)
    if not fresh.empty:
        info["stars_html_approx_pct"] = round(100 * float(fresh["stars_html_approx"].mean()), 2)
    reasons = {}
    for r in rejects:
        reasons[r["reason"][:120]] = reasons.get(r["reason"][:120], 0) + 1
    result = {"run_id": ctx.run_id, "verdict": verdict, "metrics": metrics, "info": info,
              "reject_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:10]),
              "unenriched_keys_sample": sorted(resolved - (set(enriched["repo_key"]) if not enriched.empty else set()))[:20]}
    write_json_atomic(DIRS["reports"] / f"quality_{ctx.run_id}.json", result)
    ctx.log("quality_evaluated", verdict=verdict, **{m["metric"]: m["value"] for m in metrics})
    return result


def format_quality(q: dict) -> str:
    lines = [f"{'метрика':50} {'значение':>10} {'зелёный/жёлтый':>16}  статус"]
    for m in q["metrics"]:
        v = "н/д" if m["value"] is None else m["value"]
        sign = "≥" if m["direction"] == "higher" else "≤"
        lines.append(f"{m['title']:50} {v!s:>10} {sign + str(m['green']) + ' / ' + sign + str(m['yellow']):>16}  {m['status']}")
    lines.append(f"Вердикт: {q['verdict'].upper()}")
    return "\n".join(lines)
