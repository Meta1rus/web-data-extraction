"""Наблюдаемость (п. 4.6): структурные JSON-логи, отчёт о прогоне, пороги оповещений.

Одна строка журнала = один JSON-объект. Обязательные поля есть в КАЖДОЙ строке
(если неприменимо — null): ts, run_id, event, host, url, status, ms, attempt, bytes, error.
Текстовых логов сборщик не пишет (журнал Scrapy уровня WARNING — только для человека).

События: run_start, robots, request (каждая сетевая попытка: ответ или исключение),
retry, giveup, limiter_wait, breaker, breaker_reject, autothrottle, incident, not_modified,
incremental_skip, item_dropped, domain_blocked, run_end.
"""
from __future__ import annotations

import json
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE_FIELDS = ("ts", "run_id", "event", "host", "url", "status", "ms", "attempt", "bytes", "error")


class JsonLogger:
    def __init__(self, path: str | Path, run_id: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, event: str, **fields) -> dict:
        rec = {k: None for k in BASE_FIELDS}
        rec.update(ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                   run_id=self.run_id, event=event)
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
        return rec

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()


def read_log(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def build_run_report(events: list[dict], scrapy_stats: dict | None = None, extra: dict | None = None) -> dict:
    """Метрики прогона из журнала (+ счётчики записей из статистики Scrapy)."""
    st = scrapy_stats or {}
    reqs = [e for e in events if e["event"] == "request"]
    responses = [e for e in reqs if e.get("status") is not None]
    codes: dict[str, int] = {}
    for e in reqs:
        key = str(e["status"]) if e.get("status") is not None else f"exception:{e.get('error')}"
        codes[key] = codes.get(key, 0) + 1
    n = len(reqs)
    errors = sum(1 for e in reqs if e.get("status") is None or e["status"] >= 400)
    lat = [e["ms"] for e in responses if e.get("ms") is not None]
    wire = sum(e.get("bytes") or 0 for e in responses)
    ts = [e["ts"] for e in events]
    dur = None
    if ts:
        t0 = datetime.fromisoformat(min(ts))
        t1 = datetime.fromisoformat(max(ts))
        dur = round((t1 - t0).total_seconds(), 2)
    at = [e.get("delay_ms") for e in events if e["event"] == "autothrottle" and e.get("delay_ms") is not None]
    waits = [e.get("wait_ms") for e in events if e["event"] == "limiter_wait"]
    report = {
        "run_id": events[0]["run_id"] if events else None,
        "requests": n,
        "status_counts": dict(sorted(codes.items())),
        "error_rate": round(errors / n, 4) if n else 0.0,
        "share_429_403": round(sum(1 for e in reqs if e.get("status") in (403, 429)) / n, 4) if n else 0.0,
        "retries": sum(1 for e in events if e["event"] == "retry"),
        "retry_sources": {s: sum(1 for e in events if e["event"] == "retry" and e.get("delay_source") == s)
                          for s in ("retry-after", "backoff")},
        "giveups": sum(1 for e in events if e["event"] == "giveup"),
        "latency_median_ms": round(statistics.median(lat), 1) if lat else None,
        "latency_p95_ms": round(_pct(lat, 0.95), 1) if lat else None,
        "traffic_bytes": wire,
        "traffic_kb": round(wire / 1024, 1),
        "avg_response_bytes": round(wire / len(responses), 1) if responses else 0,
        "not_modified_304": sum(1 for e in reqs if e.get("status") == 304),
        "incremental_skipped": sum(1 for e in events if e["event"] == "incremental_skip"),
        "breaker_activations": sum(1 for e in events if e["event"] == "breaker" and e.get("to_state") == "open"),
        "breaker_rejected": sum(1 for e in events if e["event"] == "breaker_reject"),
        "incidents": sum(1 for e in events if e["event"] == "incident"),
        "incidents_block": sum(1 for e in events if e["event"] == "incident" and e.get("verdict") == "блокировка"),
        "blocked_domains": sorted({e["host"] for e in events if e["event"] == "domain_blocked"}),
        "limiter_waits": len(waits),
        "limiter_wait_median_ms": round(statistics.median(waits), 1) if waits else 0,
        "autothrottle_delay_ms": {"min": min(at), "max": max(at), "last": at[-1]} if at else None,
        "items_scraped": st.get("item_scraped_count", 0),
        "items_dropped": st.get("item_dropped_count", 0),
        "duration_s": dur,
    }
    if extra:
        report.update(extra)
    return report


@dataclass(frozen=True)
class Threshold:
    key: str
    op: str          # ">" или "<"
    value: float
    severity: str    # "жёлтый" / "красный"
    why: str


THRESHOLDS = [
    Threshold("error_rate", ">", 0.05, "красный",
              "больше 5 % ошибок — сбор неполный; на учебной площадке норма — 0"),
    Threshold("share_429_403", ">", 0.01, "жёлтый",
              "429/403 — сайт просит снизить нагрузку или ограничил доступ; вежливый сборщик их почти не получает"),
    Threshold("latency_p95_ms", ">", 3000, "жёлтый",
              "p95 > 3 с — сервер перегружен (в том числе нами): лимит надо снизить"),
    Threshold("retries_per_request", ">", 0.10, "жёлтый",
              "больше одного повтора на 10 запросов — нестабильность или слишком высокая скорость"),
    Threshold("breaker_activations", ">", 0, "жёлтый",
              "срабатывание предохранителя — домен был недоступен, часть URL отложена"),
    Threshold("incidents_block", ">", 0, "красный",
              "детектор нашёл заглушку/блокировку — сбор домена остановлен, нужен разбор"),
    Threshold("items_vs_median", "<", 0.80, "красный",
              "записей меньше 80 % медианы прошлых прогонов — поломка селекторов или тихая блокировка"),
    Threshold("avg_bytes_vs_median", "<", 0.70, "красный",
              "средний ответ меньше 70 % медианы — сайт отдаёт заглушки или урезанные страницы"),
]


def evaluate_alerts(report: dict, history_cmp: dict | None = None,
                    thresholds: list[Threshold] = THRESHOLDS) -> tuple[list[dict], str]:
    """Список сработавших оповещений и итоговый статус прогона: зелёный / жёлтый / красный."""
    values = dict(report)
    n = report.get("requests") or 0
    values["retries_per_request"] = (report.get("retries", 0) / n) if n else 0.0
    if history_cmp and history_cmp.get("checks"):
        for c in history_cmp["checks"]:
            key = "items_vs_median" if c["проверка"].startswith("записей") else "avg_bytes_vs_median"
            values[key] = c["значение"]
    alerts = []
    for t in thresholds:
        v = values.get(t.key)
        if v is None:
            continue
        fired = v > t.value if t.op == ">" else v < t.value
        if fired:
            alerts.append({"порог": f"{t.key} {t.op} {t.value}", "значение": v,
                           "уровень": t.severity, "обоснование": t.why})
    status = "красный" if any(a["уровень"] == "красный" for a in alerts) else (
        "жёлтый" if alerts else "зелёный")
    return alerts, status
