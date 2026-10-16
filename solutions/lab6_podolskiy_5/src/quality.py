"""Метрики качества набора, пороги «светофора», вердикт и стоп-кран перед mart.

    python -m src.quality            # посчитать, напечатать, дописать историю в logs/

Метрики (10 шт., из них 7 обязательных по заданию отмечены *):
 M1* полнота обязательных полей          M6* согласованность бизнес-правил
 M2* полнота необязательных полей        M7  покрытие HTML-источником
 M3* уникальность ключа                  M8* актуальность (возраст данных)
 M4* валидность (по объектам)            M9* стабильность объёма к прошлому прогону
 M5* доля отбраковки + причины           M10 ссылочная целостность событий
Красный статус любой метрики → enforce_gate() бросает QualityGateError, и слой
mart не обновляется (src/mart.py вызывает gate до записи).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from . import config
from .contract import CLOCK_SKEW

GREEN, YELLOW, RED, NA = "зелёный", "жёлтый", "красный", "н/д"


class QualityGateError(RuntimeError):
    """Красный статус — загрузка в mart прерывается."""


@dataclass(frozen=True)
class Threshold:
    green: float
    yellow: float
    higher_is_better: bool
    why: str


THRESHOLDS: dict[str, Threshold] = {
    "M1 полнота обязательных полей, %": Threshold(
        100, 99.5, True, "контракт гарантирует 100 %; любое отклонение — ошибка конвейера, а не данных"),
    "M2 полнота необязательных полей, %": Threshold(
        75, 50, True, "часть пропусков естественна (≈40 % issues без меток), но провал ниже 50 % означает "
                      "сломанный селектор или изменённый ответ API"),
    "M3 уникальность ключа, %": Threshold(
        100, 99.9, True, "после шага 5 дублей быть не должно; иначе UPSERT схлопнет разные объекты"),
    "M4 валидность по объектам, %": Threshold(
        90, 80, True, "доля различных ключей raw, дошедших до clean; синтетика заведомо даёт ≈2–3 % потерь, "
                      "поломка парсера — десятки процентов"),
    "M5 доля отбраковки (по сырым записям), %": Threshold(
        5, 10, False, "в raw намеренно 5–10 % испорченных записей, из них отбраковываются ≈половина → ожидаем "
                      "3–5 %; больше 10 % — новый тип дефекта в источнике"),
    "M6 согласованность бизнес-правил, %": Threshold(
        100, 99, True, "правила уже применены на шаге 6; повторная проверка ловит регресс кода очистки"),
    "M7 покрытие HTML-источником, % репозиториев": Threshold(
        90, 70, True, "HTML даёт поля, которых нет в API; потеря страницы у 1 из 12 репозиториев (92 %) "
                      "терпима, у 4 и более — поломка разметки или блокировка"),
    "M8 актуальность: возраст данных, ч": Threshold(
        24, 72, False, "сбор планируется ежедневно; трое суток без обновления — сбой расписания"),
    "M9 стабильность объёма: падение к прошлому прогону, %": Threshold(
        10, 30, False, "clean накапливает события, поэтому нормален только рост; падение > 30 % — потеря "
                       "источника или репозитория; первый прогон — н/д (нет базы сравнения)"),
    "M10 ссылочная целостность событий, %": Threshold(
        100, 99.5, True, "событие без репозитория не загрузится в БД (FOREIGN KEY)"),
}


def status(name: str, value: float | None) -> str:
    if value is None or pd.isna(value):
        return NA
    t = THRESHOLDS[name]
    if t.higher_is_better:
        return GREEN if value >= t.green else (YELLOW if value >= t.yellow else RED)
    return GREEN if value <= t.green else (YELLOW if value <= t.yellow else RED)


def _pct(x: float) -> float:
    return round(float(x) * 100, 2)


def _rules_ok(events: pd.DataFrame, repos: pd.DataFrame) -> pd.Series:
    """Повторная проверка правил шага 6 на чистом слое (регресс-тест очистки)."""
    is_rel = events["event_type"] == "release"
    closed, created = events["closed_at"], events["created_at"]
    ok = (created <= events["collected_at"] + CLOCK_SKEW)
    ok &= closed.isna() | (closed >= created)
    ok &= is_rel | ((events["state"] == "closed") == closed.notna())
    ok &= events["merged_at"].isna() | ((events["event_type"] == "pull_request") & (events["merged_at"] >= created))
    ok &= events["comments"].isna() | (events["comments"].fillna(0) >= 0)
    ok &= events["gh_updated_at"].isna() | (events["gh_updated_at"] >= created)
    r_ok = (repos[["stars", "forks", "open_issues_prs"]] >= 0).all(axis=1)
    return pd.concat([ok.astype(bool), r_ok.astype(bool)], ignore_index=True)


def compute(events: pd.DataFrame, repos: pd.DataFrame, summary: dict, rejects: list[dict],
            previous: dict | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    api_ev = events[events["source"] == "github_api"]
    rel = events[events["event_type"] == "release"]

    req_ev = ["event_key", "repo_key", "event_type", "title", "state", "created_at", "collected_at", "url",
              "source_url"]
    req_repo = ["repo_key", "github_id", "stars", "forks", "open_issues_prs", "created_at", "collected_at"]
    full_req = pd.concat([events[req_ev].notna().all(axis=1), repos[req_repo].notna().all(axis=1)])

    optional = {   # поле → (таблица, к каким строкам применимо)
        "events.labels (непустые)": api_ev["labels"].map(lambda v: v is not None and len(v) > 0),
        "events.comments": api_ev["comments"].notna(),
        "events.author_association": api_ev["author_association"].notna(),
        "events.gh_updated_at": api_ev["gh_updated_at"].notna(),
        "releases.commit_sha": rel["commit_sha"].notna(),
        "repos.description": repos["description"].notna(),
        "repos.homepage": repos["homepage"].notna(),
        "repos.watchers": repos["watchers"].notna(),
        "repos.releases_count": repos["releases_count"].notna(),
        "repos.used_by": repos["used_by"].notna(),
        "repos.contributors": repos["contributors"].notna(),
        "repos.primary_language_pct": repos["primary_language_pct"].notna(),
    }
    optional_pct = {k: _pct(v.mean()) for k, v in optional.items() if len(v)}

    raw_total = summary["raw"]["всего строк"]
    distinct = summary["raw"].get("различных ключей", {})
    distinct_total = sum(distinct.values()) or raw_total
    reject_reasons = pd.Series([f"{r['stage']} · {r['field']}" for r in rejects]).value_counts()
    n_now = len(events)
    n_prev = (previous or {}).get("n_events")
    # падение объёма относительно прошлого прогона; рост естественен (clean накапливает события)
    volume = None if not n_prev else round(max(0.0, (n_prev - n_now) / n_prev * 100), 2)

    values = {
        "M1 полнота обязательных полей, %": _pct(full_req.mean()),
        "M2 полнота необязательных полей, %": round(sum(optional_pct.values()) / max(1, len(optional_pct)), 2),
        "M3 уникальность ключа, %": min(_pct(events["event_key"].nunique() / max(1, len(events))),
                                        _pct(repos["repo_key"].nunique() / max(1, len(repos)))),
        "M4 валидность по объектам, %": _pct((len(events) + len(repos)) / max(1, distinct_total)),
        "M5 доля отбраковки (по сырым записям), %": _pct(len(rejects) / max(1, raw_total)),
        "M6 согласованность бизнес-правил, %": _pct(_rules_ok(events, repos).mean()),
        "M7 покрытие HTML-источником, % репозиториев": _pct(
            (repos["source"] == "github_api+github_html").mean()) if len(repos) else None,
        "M8 актуальность: возраст данных, ч": round(
            (now - events["collected_at"].max().to_pydatetime()).total_seconds() / 3600, 2),
        "M9 стабильность объёма: падение к прошлому прогону, %": volume,
        "M10 ссылочная целостность событий, %": _pct(events["repo_key"].isin(set(repos["repo_key"])).mean()),
    }
    rows = []
    for name, value in values.items():
        t = THRESHOLDS[name]
        sign = "≥" if t.higher_is_better else "≤"
        rows.append({"метрика": name, "значение": value,
                     "зелёный": f"{sign} {t.green:g}", "жёлтый": f"{sign} {t.yellow:g}",
                     "статус": status(name, value), "обоснование порога": t.why})
    table = pd.DataFrame(rows)
    statuses = [s for s in table["статус"] if s != NA]
    verdict = ("НЕ ПРИГОДЕН" if RED in statuses else
               "ПРИГОДЕН С ЗАМЕЧАНИЯМИ" if YELLOW in statuses else "ПРИГОДЕН")
    return {"computed_at": now.isoformat(timespec="seconds"), "n_events": n_now, "n_repos": len(repos),
            "n_prev": n_prev, "table": table, "verdict": verdict, "optional_detail": optional_pct,
            "reject_reasons": reject_reasons}


def enforce_gate(report: dict) -> None:
    """Стоп-кран: красная метрика → исключение с перечнем причин."""
    red = report["table"][report["table"]["статус"] == RED]
    if len(red):
        reasons = "; ".join(f"{r['метрика']} = {r['значение']} (порог жёлтого {r['жёлтый']})"
                            for _, r in red.iterrows())
        raise QualityGateError(f"Вердикт «{report['verdict']}». Загрузка в mart остановлена: {reasons}")


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame, dict, list[dict]]:
    events = pd.read_parquet(config.CLEAN_EVENTS)
    repos = pd.read_parquet(config.CLEAN_REPOS)
    summary = json.loads((config.LOG_DIR / "clean_summary.json").read_text(encoding="utf-8"))
    rejects = [json.loads(line) for line in config.CLEAN_REJECTS.read_text(encoding="utf-8").splitlines()
               if line.strip()] if config.CLEAN_REJECTS.exists() else []
    return events, repos, summary, rejects


HISTORY = "quality_history.jsonl"


def previous_run() -> dict | None:
    path = config.LOG_DIR / HISTORY
    if not path.exists():
        return None
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return lines[-1] if lines else None


def save(report: dict) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    report["table"].to_csv(config.LOG_DIR / "quality_latest.csv", index=False, encoding="utf-8")
    entry = {"computed_at": report["computed_at"], "n_events": report["n_events"], "n_repos": report["n_repos"],
             "verdict": report["verdict"],
             "metrics": dict(zip(report["table"]["метрика"], report["table"]["значение"]))}
    with (config.LOG_DIR / HISTORY).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    (config.LOG_DIR / "quality_latest.json").write_text(json.dumps(
        {**entry, "statuses": dict(zip(report["table"]["метрика"], report["table"]["статус"])),
         "optional_detail": report["optional_detail"],
         "reject_reasons": report["reject_reasons"].to_dict()}, ensure_ascii=False, indent=2), encoding="utf-8")


def run(record: bool = True) -> dict:
    events, repos, summary, rejects = load_inputs()
    report = compute(events, repos, summary, rejects, previous_run())
    if record:
        save(report)
    return report


def demo_red_gate() -> dict:
    """Демонстрация стоп-крана: имитируем поломку HTML-парсера (все страницы без данных)."""
    events, repos, summary, rejects = load_inputs()
    broken = repos.copy()
    html_cols = ["releases_count", "latest_release_tag", "latest_release_at", "used_by", "contributors",
                 "contributors_is_lower_bound", "primary_language_pct", "stars_html", "html_collected_at",
                 "html_source_url"]
    broken[html_cols] = None
    broken["source"] = "github_api"
    report = compute(events[events["source"] == "github_api"], broken, summary, rejects,
                     previous={"n_events": len(events)})
    try:
        enforce_gate(report)
        outcome = "gate пропустил (плохо)"
    except QualityGateError as exc:
        outcome = str(exc)
    return {"report": report, "gate": outcome}


def main() -> int:
    report = run()
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 60)
    print(report["table"].drop(columns=["обоснование порога"]).to_string(index=False))
    print("\nПричины отбраковки:\n" + report["reject_reasons"].to_string())
    print(f"\nВердикт: {report['verdict']}")
    try:
        enforce_gate(report)
    except QualityGateError as exc:
        print("⛔", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
