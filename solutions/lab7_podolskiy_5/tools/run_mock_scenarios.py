#!/usr/bin/env python3
"""Воспроизведение сценариев отказа на ЛОКАЛЬНОЙ имитации (tools/mock_shop.py).

Сценарии, которые нельзя (и нельзя этически) вызвать на реальном сайте, воспроизводятся
на собственном стенде: 429 с Retry-After (секунды/дата/без заголовка), рост латентности
(срабатывание AutoThrottle), «тихая» заглушка-капча с кодом 200, авария второго домена
(предохранитель), прерывание и возобновление прогона. Здесь же прогоны A/B/C и замер
экономии трафика на одинаковом объёме.

Все отчёты получают метку mock_* и флаг local_mock=true: в отчёт лабораторной цифры
отсюда идут ТОЛЬКО с пометкой «(на локальной имитации)».

Запуск:  python3 tools/run_mock_scenarios.py            # все сценарии, ~6–8 мин
         python3 tools/run_mock_scenarios.py --only 429,resume
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
SHOP_PORT = int(os.environ.get("MOCK_PORT", "18507"))
ALT_PORT = SHOP_PORT + 1
SHOP = f"http://127.0.0.1:{SHOP_PORT}"
ALT = f"http://localhost:{ALT_PORT}"            # другое имя хоста = другой домен для Scrapy и предохранителя
RESULTS: dict = {}


def wait_port(port: int, timeout: float = 10.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"имитация на порту {port} не запустилась")


class Mock:
    def __init__(self, port: int, *flags: str):
        self.port = port
        self.proc = subprocess.Popen([PY, str(ROOT / "tools" / "mock_shop.py"), "--port", str(port), *flags],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_port(port)

    def admin(self, path: str) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return json.loads(r.read().decode())

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def collect(label: str, *args: str, timeout: int = 600) -> dict:
    cmd = [PY, "-m", "src.collect", "--label", label, "--local-mock", *args]
    print("$", " ".join(cmd[2:]), flush=True)
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    tail = "\n".join(proc.stdout.strip().splitlines()[-4:])
    print(f"  код возврата {proc.returncode}; {tail}", flush=True)
    rep_path = ROOT / "reports" / f"run_report_{label}.json"
    rep = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}
    rep["_exit_code"] = proc.returncode
    RESULTS[label] = {k: rep.get(k) for k in ("requests", "status_counts", "error_rate", "retries",
                                               "traffic_kb", "not_modified_304", "incremental_skipped",
                                               "breaker_activations", "incidents", "items_scraped",
                                               "duration_s", "status", "_exit_code")}
    return rep


def scenario_abc():
    """A/B/C на одинаковом объёме (1 стр. каталога + 16 карточек). Сервер ограничивает 5 запр/с."""
    with Mock(SHOP_PORT, "--ratelimit", "5:5", "--retry-after-mode", "cycle"):
        collect("mock_A", "--mode", "A", "--pages", "1", "--start-url", f"{SHOP}/ecommerce/", "--fresh-state",
                "--max-requests", "30", "--no-history")
        time.sleep(2)
        collect("mock_B", "--mode", "B", "--pages", "1", "--start-url", f"{SHOP}/ecommerce/", "--fresh-state",
                "--no-history")
        collect("mock_C", "--mode", "C", "--pages", "1", "--start-url", f"{SHOP}/ecommerce/", "--no-history")


def scenario_savings():
    """Экономия по механизмам: условные запросы, gzip, инкрементальность (+ изменение данных)."""
    with Mock(SHOP_PORT) as m:
        url = f"{SHOP}/ecommerce/"
        collect("mock_S0_base", "--mode", "B", "--pages", "1", "--start-url", url, "--fresh-state", "--no-history")
        collect("mock_S1_cond", "--mode", "B", "--pages", "1", "--start-url", url, "--cond", "--no-history")
        collect("mock_S2_gzip", "--mode", "B", "--pages", "1", "--start-url", url, "--gzip", "--no-history")
        collect("mock_S3_incr", "--mode", "B", "--pages", "1", "--start-url", url, "--cond", "--incremental",
                "--no-history")
        changed = m.admin("/__mock/mutate?n=3")
        RESULTS["mock_mutated_products"] = changed
        collect("mock_S4_after_change", "--mode", "C", "--pages", "1", "--start-url", url, "--no-history")


def scenario_429():
    """429 от домена: сервер 3 запр/с, мы намеренно 8 запр/с → Retry-After (секунды, дата, нет)."""
    with Mock(SHOP_PORT, "--ratelimit", "3:2", "--retry-after-mode", "cycle"):
        collect("mock_429", "--mode", "B", "--pages", "2", "--start-url", f"{SHOP}/ecommerce/", "--fresh-state",
                "--rate", "8", "--burst", "8", "--no-history")
        collect("mock_429_polite", "--mode", "B", "--pages", "2", "--start-url", f"{SHOP}/ecommerce/",
                "--fresh-state", "--rate", "2", "--burst", "2", "--no-history")


def scenario_autothrottle():
    """Рост латентности с 10-го по 30-й запрос (+600 мс): AutoThrottle поднимает задержку и снижает обратно."""
    with Mock(SHOP_PORT, "--slow-from", "10", "--slow-to", "30", "--slow-ms", "600"):
        collect("mock_autothrottle", "--mode", "B", "--pages", "3", "--start-url", f"{SHOP}/ecommerce/",
                "--fresh-state", "--rate", "4", "--burst", "2", "--no-history")


def scenario_resume():
    """Прерывание (SIGINT) и возобновление из JOBDIR — общий скрипт tools/resume_demo.py."""
    sys.path.insert(0, str(ROOT / "tools"))
    import resume_demo
    with Mock(SHOP_PORT):
        summary = resume_demo.main(["--start-url", f"{SHOP}/ecommerce/", "--pages", "3", "--after", "8",
                                    "--prefix", "mock_resume", "--local-mock"])
    RESULTS["mock_resume"] = summary


def scenario_breaker():
    """Авария второго домена (503 первые 8 с): предохранитель изолирует его, каталог собирается дальше."""
    with Mock(SHOP_PORT), Mock(ALT_PORT, "--outage-seconds", "8"):
        collect("mock_breaker", "--mode", "B", "--pages", "3", "--start-url", f"{SHOP}/ecommerce/",
                "--fresh-state", "--extra-url", f"{ALT}/status/200", "--breaker-reset", "5", "--no-history")


def scenario_detector_history():
    """Три нормальных прогона → прогон с «тихой» заглушкой-капчей (200) на 2-й странице каталога."""
    with Mock(SHOP_PORT) as m:
        url = f"{SHOP}/ecommerce/"
        for i in (1, 2, 3):
            collect(f"mock_hist_{i}", "--mode", "B", "--pages", "2", "--start-url", url, "--fresh-state",
                    "--profile", "mock-history", "--rate", "4", "--burst", "4")
        m.admin("/__mock/config?captcha_paths=" + quote("^/ecommerce/page/2/"))
        collect("mock_hist_captcha", "--mode", "B", "--pages", "2", "--start-url", url, "--fresh-state",
                "--profile", "mock-history", "--rate", "4", "--burst", "4")


def scenario_probes():
    """Коды ответа «как на httpbin»: 429 (с/без Retry-After), 401, 403, 404, ETag/304, gzip, 503, таймаут."""
    with Mock(SHOP_PORT), Mock(ALT_PORT):
        date = quote(formatdate(time.time() + 4, usegmt=True))
        codes = "|".join([f"{SHOP}/status/429?retry_after=2", f"{SHOP}/status/429", f"{SHOP}/status/429?retry_after={date}",
                          f"{SHOP}/status/401", f"{SHOP}/status/403", f"{SHOP}/status/404", f"{SHOP}/etag/lab7-v1",
                          f"{SHOP}/gzip", f"{SHOP}/response-headers?Retry-After=3"])
        collect("mock_probe_codes", "--probe-urls", codes, "--cond", "--gzip", "--fresh-state", "--no-history")
        collect("mock_probe_etag_repeat", "--probe-urls", f"{SHOP}/etag/lab7-v1", "--cond", "--no-history")
        fail = "|".join([f"{ALT}/status/503", f"{ALT}/delay/8", f"{ALT}/status/503?x=2", f"{ALT}/status/200"])
        collect("mock_probe_503_timeout", "--probe-urls", fail, "--timeout", "3", "--breaker-reset", "5",
                "--no-history")


def scenario_robots():
    """robots.txt: копия настоящего файла scrapingcourse.com (Disallow: /ecommerce/*) и случай 404."""
    with Mock(SHOP_PORT, "--robots", "real"):
        collect("mock_robots_real", "--mode", "B", "--pages", "1", "--start-url", f"{SHOP}/ecommerce/",
                "--no-history")
    with Mock(SHOP_PORT, "--robots", "none"):
        cmd = [PY, "-m", "src.collect", "--robots-only", "--start-url", f"{SHOP}/ecommerce/"]
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
        RESULTS["mock_robots_404"] = {"exit_code": out.returncode, "stdout": out.stdout.strip().splitlines()}


SCENARIOS = {"abc": scenario_abc, "savings": scenario_savings, "429": scenario_429,
             "autothrottle": scenario_autothrottle, "resume": scenario_resume, "breaker": scenario_breaker,
             "history": scenario_detector_history, "probes": scenario_probes, "robots": scenario_robots}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="через запятую: " + ",".join(SCENARIOS))
    args = ap.parse_args()
    names = args.only.split(",") if args.only else list(SCENARIOS)
    t0 = time.monotonic()
    for name in names:
        print(f"\n### сценарий {name}", flush=True)
        SCENARIOS[name]()
    RESULTS["_total_seconds"] = round(time.monotonic() - t0, 1)
    out = ROOT / "reports" / "mock_scenarios_summary.json"
    prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    prev.update(RESULTS)
    out.write_text(json.dumps(prev, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nСводка: {out.relative_to(ROOT)} ({RESULTS['_total_seconds']} с)")


if __name__ == "__main__":
    main()
