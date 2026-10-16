"""Наблюдаемость: структурные JSON-логи, инциденты, отчёт о прогоне и пороги оповещения.

Каждый HTTP-запрос и каждое ключевое событие пишутся одной строкой JSON в logs/run_<run_id>.jsonl.
Отчёт о прогоне строится ТОЛЬКО из этого журнала и счётчиков этапов — никаких «ручных» цифр.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from settings import DIRS, INCIDENTS_DIR, iso, utcnow, write_json_atomic

LEVEL_ORDER = {"green": 0, "yellow": 1, "red": 2}


class RunContext:
    """Идентификатор прогона, журнал и регистрация инцидентов."""

    def __init__(self, run_id: str | None = None, echo: bool = True):
        self.started_at = utcnow()
        self.run_id = run_id or self.started_at.strftime("%Y%m%dT%H%M%SZ")
        self.t0 = time.monotonic()
        self.log_path = DIRS["logs"] / f"run_{self.run_id}.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self.incidents: list[dict] = []
        self.counters: Counter = Counter()

    def log(self, event: str, level: str = "INFO", **fields: Any) -> dict:
        rec = {"ts": utcnow().isoformat(timespec="milliseconds"), "run_id": self.run_id,
               "level": level, "event": event, **fields}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def say(self, message: str) -> None:
        """Короткое сообщение в консоль (для человека); машиночитаемая информация — в журнале."""
        if self.echo:
            print(message, flush=True)

    def incident(self, kind: str, message: str, **context: Any) -> dict:
        """Инцидент: отдельный JSON-файл в logs/incidents/ + событие ERROR в журнале."""
        n = len(self.incidents) + 1
        record = {"run_id": self.run_id, "n": n, "kind": kind, "message": message,
                  "ts": iso(utcnow()), **context}
        path = INCIDENTS_DIR / f"{self.run_id}_{n:02d}_{kind}.json"
        write_json_atomic(path, record)
        record["path"] = f"logs/incidents/{path.name}"
        self.incidents.append(record)
        self.log("incident", level="ERROR", kind=kind, message=message, path=record["path"])
        self.say(f"[ИНЦИДЕНТ] {kind}: {message}")
        return record

    def elapsed_s(self) -> float:
        return round(time.monotonic() - self.t0, 2)


# ---------------------------------------------------------------- отчёт о прогоне

def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def request_stats(rows: list[dict]) -> dict:
    req = [r for r in rows if r.get("event") == "request"]
    by_status = Counter("network_error" if r.get("status") is None else str(r["status"]) for r in req)
    by_host = Counter(r.get("host") for r in req)
    by_source = Counter(r.get("source") for r in req)
    lat = [r["ms"] for r in req if isinstance(r.get("ms"), (int, float))]
    errors = [r for r in req if r.get("status") is None or r["status"] >= 400]
    limited = [r for r in req if r.get("status") in (403, 429)]
    body = sum(r.get("bytes") or 0 for r in req)
    known = [r for r in req if r.get("wire_bytes")]          # ответы, для которых известен размер по сети
    wire = sum(r["wire_bytes"] for r in known)
    body_known = sum(r.get("bytes") or 0 for r in known)

    # Квота GitHub API по заголовкам: расход между соседними запросами одного ресурса квоты.
    api = [r for r in req if r.get("rl_used") is not None]
    used_on: Counter = Counter()
    prev_by_res: dict[str, int] = {}
    for r in api:
        res = r.get("rl_resource") or "core"
        if res in prev_by_res:
            delta = max(0, int(r["rl_used"]) - prev_by_res[res])
            # запрос с редиректом (переименованный репозиторий) расходует квоту на 301 — учитываем отдельно
            used_on["redirect" if r.get("redirected") else str(r.get("status"))] += delta
        prev_by_res[res] = int(r["rl_used"])
    core = [r for r in api if (r.get("rl_resource") or "core") == "core"]
    quota = None
    if core:
        first, last = core[0], core[-1]
        limit = int(last.get("rl_limit") or 0)
        quota = {
            "limit": limit,
            "remaining_first": first.get("rl_remaining"),
            "remaining_last": last.get("rl_remaining"),
            "remaining_last_pct": round(100 * int(last.get("rl_remaining") or 0) / limit, 1) if limit else None,
            "used_delta_by_status": dict(used_on),
            "responses_304": sum(1 for r in core if r.get("status") == 304),
        }
    return {
        "total": len(req),
        "by_status": dict(sorted(by_status.items())),
        "by_host": dict(by_host),
        "by_source": dict(by_source),
        "error_rate_pct": round(100 * len(errors) / len(req), 2) if req else 0.0,
        "rate_limited_pct": round(100 * len(limited) / len(req), 2) if req else 0.0,
        "retries": sum(1 for r in req if (r.get("attempt") or 1) > 1),
        "not_modified_304": by_status.get("304", 0),
        "conditional_sent": sum(1 for r in req if r.get("conditional")),
        "network_errors": by_status.get("network_error", 0),
        "latency_ms": {"median": round(statistics.median(lat), 1) if lat else None,
                       "p95": _pct(lat, 0.95), "max": max(lat) if lat else None},
        "traffic": {"body_bytes": body, "wire_bytes": wire, "wire_known_responses": len(known),
                    "gzip_saving_pct": round(100 * (1 - wire / body_known), 1) if body_known and wire else None},
        "api_quota": quota,
        "cache_hits": sum(1 for r in rows if r.get("event") == "cache_hit"),
        "skipped_by_breaker": sum(1 for r in rows if r.get("event") == "request_skipped"
                                  and r.get("reason") == "breaker_open"),
        "breaker_activations": sum(1 for r in rows if r.get("event") == "breaker_state"
                                   and r.get("state") == "open"),
        "robots_blocked": sum(1 for r in rows if r.get("event") == "request_skipped"
                              and r.get("reason") == "robots"),
    }


def evaluate_alerts(report: dict, prev: dict | None, history: list[dict], cfg: dict) -> list[dict]:
    """Пороги оповещения. Возвращает список сработавших оповещений (пустой список — прогон здоров)."""
    th = cfg["alerts"]
    req = report["requests"]
    fired: list[dict] = []

    def check(code: str, value: float | None, higher_is_worse: bool, message: str) -> None:
        if value is None:
            return
        lim = th[code]
        bad = (lambda v, t: v >= t) if higher_is_worse else (lambda v, t: v <= t)
        level = "red" if bad(value, lim["red"]) else ("yellow" if bad(value, lim["yellow"]) else None)
        if level:
            fired.append({"level": level, "code": code, "value": value,
                          "threshold": lim[level], "message": message.format(v=value, t=lim[level])})

    check("error_rate_pct", req["error_rate_pct"], True, "доля ошибок HTTP {v}% (порог {t}%)")
    check("rate_limited_pct", req["rate_limited_pct"], True,
          "ответы 403/429: {v}% запросов (порог {t}%) — сбавить скорость, проверить квоту")
    check("latency_p95_ms", req["latency_ms"]["p95"], True, "латентность p95 {v} мс (порог {t} мс)")
    check("breaker_activations", req["breaker_activations"], True,
          "предохранитель размыкался {v} раз(а) (порог {t})")
    quota = req.get("api_quota") or {}
    check("quota_remaining_pct", quota.get("remaining_last_pct"), False,
          "остаток квоты GitHub API {v}% (порог {t}%)")

    # сравнение объёма с предыдущим успешным прогоном
    cards = report["records"].get("html_cards", 0)
    prev_cards = (prev or {}).get("records", {}).get("html_cards")
    if prev_cards:
        drop = round(100 * (prev_cards - cards) / prev_cards, 1) if cards < prev_cards else 0.0
        report["records"]["html_cards_prev"] = prev_cards
        report["records"]["html_cards_drop_pct"] = drop
        check("html_cards_drop_pct", drop if drop > 0 else None, True,
              "карточек HTML на {v}% меньше, чем в прошлом прогоне (порог {t}%) — поломка селекторов или блокировка?")
    if report["records"].get("html_pages_ok", 0) > 0 and cards == 0:
        fired.append({"level": "red", "code": "html_no_cards", "value": 0, "threshold": 1,
                      "message": "страницы тем получены, но карточек 0 — вероятно, сменилась вёрстка"})

    # «нет новых данных» несколько прогонов подряд
    changes = report.get("storage", {}).get("repos", {})
    streak = 0
    if changes and changes.get("inserted", 0) + changes.get("updated", 0) == 0:
        streak = 1
        for h in history:
            if (h.get("inserted") or 0) + (h.get("updated") or 0) == 0:
                streak += 1
            else:
                break
    report["records"]["zero_changes_streak"] = streak
    check("zero_changes_streak", streak if streak else None, True,
          "{v} прогон(а) подряд без изменений в данных (порог {t}) — источник «застыл» или сбор не доходит до данных")

    # качество и инциденты
    q = report.get("quality", {}).get("verdict")
    if q in ("yellow", "red"):
        fired.append({"level": q, "code": "quality", "value": q, "threshold": "green",
                      "message": f"вердикт качества: {q}" + (" — загрузка в mart заблокирована" if q == "red" else "")})
    for inc in report.get("incidents", []):
        fired.append({"level": "red" if inc["kind"] in ("source_unavailable", "blocked") else "yellow",
                      "code": f"incident:{inc['kind']}", "value": inc.get("source"),
                      "threshold": "-", "message": inc["message"]})
    return fired


def overall_verdict(alerts: list[dict]) -> str:
    level = "green"
    for a in alerts:
        if LEVEL_ORDER[a["level"]] > LEVEL_ORDER[level]:
            level = a["level"]
    return level


def write_run_report(report: dict) -> Path:
    out = DIRS["reports"] / f"run_report_{report['run_id']}.json"
    write_json_atomic(out, report)
    write_json_atomic(DIRS["reports"] / "latest_run_report.json", report)
    return out


def previous_report(current_run_id: str, scope: str | None = None) -> dict | None:
    """Последний отчёт прогона, дошедшего до конца (не прерванного), кроме текущего.

    scope — «охват» прогона (темы, страницы, режимы сортировки): объём карточек HTML сравнивается
    только с прогоном того же охвата, иначе демонстрационный прогон по одной теме выглядел бы как обвал.
    """
    candidates = sorted(DIRS["reports"].glob("run_report_*.json"), reverse=True)
    for p in candidates:
        if current_run_id in p.name:
            continue
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[предупреждение] не удалось прочитать {p.name}: {e}", file=sys.stderr)
            continue
        if scope is not None and rep.get("environment", {}).get("scope") != scope:
            continue
        if rep.get("status") == "finished" and rep.get("records", {}).get("html_cards"):
            return rep
    return None


def format_report(report: dict) -> str:
    req = report["requests"]
    rec = report["records"]
    lines = [
        f"Прогон {report['run_id']} — вердикт: {report['verdict'].upper()}, код возврата {report['exit_code']}",
        f"  длительность: {report['duration_s']} c; запросов: {req['total']}; коды: {req['by_status']}",
        f"  ошибки: {req['error_rate_pct']}%; 403/429: {req['rate_limited_pct']}%; повторов: {req['retries']}; "
        f"304: {req['not_modified_304']} из {req['conditional_sent']} условных",
        f"  латентность: медиана {req['latency_ms']['median']} мс, p95 {req['latency_ms']['p95']} мс; "
        f"трафик: тело {req['traffic']['body_bytes']} Б, по сети {req['traffic']['wire_bytes']} Б (размер известен для {req['traffic'].get('wire_known_responses', 0)} ответов: у chunked-ответов GitHub его нет)",
        f"  HTML: страниц {rec.get('html_pages_ok', 0)} (+{rec.get('html_pages_resumed', 0)} из прерванного прогона), "
        f"карточек {rec.get('html_cards', 0)}; "
        f"API: 200 — {rec.get('api_200', 0)}, 304 — {rec.get('api_304', 0)}, "
        f"пропущено по водяному знаку — {rec.get('api_skipped_watermark', 0)}, 404 — {rec.get('api_404', 0)}",
        f"  чистый слой: репозиториев {rec.get('clean_repos', 0)}, карточек тем {rec.get('clean_listings', 0)}, "
        f"отбраковано {rec.get('rejects', 0)}",
    ]
    st = report.get("storage", {})
    if st:
        r, l = st.get("repos", {}), st.get("listings", {})
        lines.append(f"  UPSERT repos: +{r.get('inserted', 0)} / обновлено {r.get('updated', 0)} / без изменений "
                     f"{r.get('unchanged', 0)}; listings: +{l.get('inserted', 0)} / {l.get('updated', 0)} / "
                     f"{l.get('unchanged', 0)}")
    q = report.get("quality", {})
    if q:
        lines.append(f"  качество: {q.get('verdict')}; mart {'обновлён' if report.get('mart_built') else 'НЕ обновлялся'}")
    quota = req.get("api_quota")
    if quota:
        lines.append(f"  квота API: остаток {quota['remaining_last']} из {quota['limit']}; "
                     f"расход по кодам ответа: {quota['used_delta_by_status']}")
    for a in report.get("alerts", []):
        lines.append(f"  ОПОВЕЩЕНИЕ [{a['level']}] {a['code']}: {a['message']}")
    return "\n".join(lines)
