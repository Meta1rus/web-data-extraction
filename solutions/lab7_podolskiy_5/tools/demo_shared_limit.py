#!/usr/bin/env python3
"""Лимит при 4 параллельных процессах (п. 4.1): локальные ведра vs общий счётчик.

4 воркера делают по N запросов к одному домену с лимитом 2 запр/с:
  * «local»  — у каждого своё ведро: сайт получает до 4 × 2 = 8 запр/с (антипаттерн);
  * «shared» — общее ведро SharedTokenBucket (файл + fcntl.flock): в сумме ≤ 2 запр/с.
Скорость считается по меткам прихода запросов НА СТОРОНЕ СЕРВЕРА (имитация /__mock/stats).

Только локальная имитация: на реальный сайт «антипаттерн» не запускаем.
Запуск:  python3 tools/demo_shared_limit.py           (сам поднимает имитацию на порту 18509)
"""
from __future__ import annotations

import json
import multiprocessing as mp
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.limiter import SharedTokenBucket, TokenBucket  # noqa: E402

PORT = 18509
BASE = f"http://127.0.0.1:{PORT}"
RATE, WORKERS, PER_WORKER = 2.0, 4, 8


def worker(mode: str, idx: int, state_file: str) -> None:
    bucket = SharedTokenBucket(state_file, RATE, burst=1) if mode == "shared" else TokenBucket(RATE, burst=1)
    for i in range(PER_WORKER):
        time.sleep(bucket.reserve())             # ждём ровно столько, сколько велит ограничитель
        req = urllib.request.Request(f"{BASE}/status/200?w={idx}&i={i}",
                                     headers={"User-Agent": "PSU-edu-crawler/1.0 (shared-limit demo)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()


def admin(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read().decode())


def run(mode: str) -> dict:
    state = ROOT / "state" / "shared_limit_demo.json"
    state.unlink(missing_ok=True)
    admin("/__mock/stats?reset=1")
    procs = [mp.Process(target=worker, args=(mode, i, str(state))) for i in range(WORKERS)]
    t0 = time.monotonic()
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
    wall = time.monotonic() - t0
    arrivals = sorted(a for a in admin("/__mock/stats")["arrivals"])
    arrivals = arrivals[:-1]          # последняя метка — сам запрос /__mock/stats
    span = arrivals[-1] - arrivals[0] if len(arrivals) > 1 else 0
    max_1s = max(sum(1 for b in arrivals if a <= b < a + 1.0) for a in arrivals) if arrivals else 0
    return {"режим": mode, "воркеров": WORKERS, "лимит, запр/с": RATE, "запросов": len(arrivals),
            "время, с": round(wall, 2), "средняя скорость на сервере, запр/с": round((len(arrivals) - 1) / span, 2) if span else None,
            "максимум за любую 1 с": max_1s}


def main() -> None:
    mock = subprocess.Popen([sys.executable, str(ROOT / "tools" / "mock_shop.py"), "--port", str(PORT)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                admin("/__mock/stats")
                break
            except OSError:
                time.sleep(0.2)
        rows = [run("local"), run("shared")]
    finally:
        mock.terminate()
        mock.wait(timeout=10)
    out = ROOT / "reports" / "shared_limit.json"
    out.write_text(json.dumps({"local_mock": True, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    cols = list(rows[0])
    print("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")
    print("(на локальной имитации)")


if __name__ == "__main__":
    main()
