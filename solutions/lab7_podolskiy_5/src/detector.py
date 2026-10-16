"""Диагностика блокировок и деградации (п. 4.5).

inspect()          — вердикт по одному ответу: ok / подозрение / блокировка.
save_incident()    — артефакты подозрительного ответа: тело, заголовки, метаданные.
compare_history()  — сравнение прогона с медианой предыдущих (объём записей, средний размер ответа).

Правила вердикта (проверяется не только код, но и содержимое):
| сигнал                                           | вердикт      |
|--------------------------------------------------|--------------|
| текстовые маркеры заглушки/капчи в теле          | блокировка   |
| 403 с маркерами или 3-й подряд 403 на домене     | блокировка   |
| 403 без маркеров                                 | подозрение   |
| 429                                              | подозрение (ограничение скорости — обрабатывает политика повторов) |
| 200, но нет ожидаемых элементов страницы         | подозрение   |
| 200, но ответ аномально мал                      | подозрение   |
| маленький ответ И нет ожидаемых элементов        | блокировка (тихая заглушка) |
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BLOCK_MARKERS = [
    "captcha", "are you human", "are you a robot", "access denied", "unusual traffic",
    "request blocked", "rate limit exceeded", "cf-browser-verification", "just a moment",
    "доступ ограничен", "подтвердите, что вы не робот", "вы робот", "слишком много запросов",
]
OK, SUSPECT, BLOCK = "ok", "подозрение", "блокировка"


@dataclass
class Verdict:
    level: str
    signals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"verdict": self.level, "signals": self.signals}


def inspect(status: int, body: bytes | str, *, expected_found: bool | None = None,
            min_bytes: int = 1500, consecutive_403: int = 0, is_html: bool = True) -> Verdict:
    """Вердикт по ответу. expected_found — найдены ли ожидаемые элементы (None — не проверялось)."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    low = text[:300_000].lower()
    size = len(body)
    signals = []
    markers = [m for m in BLOCK_MARKERS if m in low]
    if markers:
        signals.append("маркеры: " + ", ".join(markers[:4]))
    if status in (403, 429):
        signals.append(f"код {status}")
    small = is_html and status == 200 and size < min_bytes
    if small:
        signals.append(f"аномально малый ответ: {size} байт < {min_bytes}")
    missing = status == 200 and expected_found is False
    if missing:
        signals.append("нет ожидаемых элементов при коде 200")

    if markers or (status == 403 and consecutive_403 >= 3) or (small and missing):
        return Verdict(BLOCK, signals)
    if signals:
        return Verdict(SUSPECT, signals)
    return Verdict(OK, [])


def save_incident(incidents_dir: str | Path, run_id: str, seq: int, *, url: str, status: int,
                  headers: dict, body: bytes, verdict: Verdict, extra: dict | None = None) -> Path:
    """Артефакты: тело ответа, заголовки, метаданные (время, URL, вердикт, сигналы)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", url.split("://", 1)[-1])[:80]
    d = Path(incidents_dir) / f"{run_id}_{seq:03d}_{safe}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "body.html").write_bytes(body)
    (d / "headers.json").write_text(json.dumps(headers, ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "run_id": run_id,
            "url": url, "status": status, "bytes": len(body), **verdict.as_dict(), **(extra or {})}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


# --------------------------------------------------------------------------- история прогонов
def load_history(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_history(path: str | Path, record: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def compare_history(current: dict, history: list[dict], *, profile: str,
                    items_min_ratio: float = 0.8, bytes_min_ratio: float = 0.7, min_runs: int = 2) -> dict:
    """Сравнение с медианой предыдущих прогонов того же профиля (источник + объём + режим).

    Красный статус: записей < 80 % медианы или средний размер ответа < 70 % медианы.
    """
    prev = [h for h in history if h.get("profile") == profile and h.get("run_id") != current.get("run_id")]
    if len(prev) < min_runs:
        return {"status": "нет истории", "previous_runs": len(prev), "checks": []}
    med_items = statistics.median(h["items"] for h in prev)
    med_bytes = statistics.median(h["avg_bytes"] for h in prev)
    checks = []
    for name, cur, med, thr in (("записей к медиане", current["items"], med_items, items_min_ratio),
                                ("средний размер ответа к медиане", current["avg_bytes"], med_bytes, bytes_min_ratio)):
        ratio = cur / med if med else 1.0
        checks.append({"проверка": name, "значение": round(ratio, 3), "порог": f"≥ {thr:.0%}",
                       "статус": "красный" if ratio < thr else "ок"})
    status = "красный" if any(c["статус"] == "красный" for c in checks) else "ок"
    return {"status": status, "previous_runs": len(prev), "median_items": med_items,
            "median_avg_bytes": med_bytes, "checks": checks}
