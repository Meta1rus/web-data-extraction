#!/usr/bin/env python3
"""Предварительная проверка robots.txt перед реальным прогоном.

Проверяет те же правила, что применит Scrapy (парсер Protego, понимает шаблоны * и $
по RFC 9309), и параллельно urllib.robotparser из стандартной библиотеки.
Важно: urllib.robotparser в Python ≤ 3.12 (и в ранних 3.13.x) НЕ понимает шаблоны,
поэтому на правило «Disallow: /ecommerce/*» ошибочно отвечает «можно». Решение
принимаем по более строгому ответу.

Код возврата: 0 — обход разрешён; 3 — запрещён robots.txt; 4 — robots.txt недоступен (5xx/сеть).
Результат дополнительно пишется в data/robots_preflight.json.

Пример:  python3 tools/robots_preflight.py --start-url https://www.scrapingcourse.com/ecommerce/
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

from protego import Protego

UA = "PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)"
ROOT = Path(__file__).resolve().parent.parent


def fetch_robots(origin: str, timeout: float = 15.0) -> tuple[int | None, str]:
    req = urllib.request.Request(origin + "/robots.txt", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"robots.txt недоступен: {e}", file=sys.stderr)
        return None, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-url", default="https://www.scrapingcourse.com/ecommerce/")
    ap.add_argument("--out", default=str(ROOT / "data" / "robots_preflight.json"))
    args = ap.parse_args()

    parts = urlsplit(args.start_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    base = args.start_url if args.start_url.endswith("/") else args.start_url + "/"
    probes = [args.start_url, urljoin(base, "page/2/"), urljoin(origin, "/ecommerce/product/abominable-hoodie/")
              if "/ecommerce" in parts.path else urljoin(base, "sample-product/")]

    status, text = fetch_robots(origin)
    if status is None or (status >= 500):
        verdict, code = "robots.txt недоступен (сеть/5xx): по RFC 9309 считаем обход запрещённым", 4
        rows = []
    else:
        lines = text.splitlines() if status == 200 else []
        rp = robotparser.RobotFileParser()
        rp.parse(lines)
        pg = Protego.parse("\n".join(lines))
        rows = []
        for url in probes:
            a, b = rp.can_fetch(UA, url), pg.can_fetch(url, UA)
            rows.append({"url": url, "urllib_robotparser": a, "protego_scrapy": b, "итог": a and b})
        allowed = all(r["итог"] for r in rows)
        code = 0 if allowed else 3
        verdict = ("обход разрешён" if allowed else
                   "обход ЗАПРЕЩЁН robots.txt — сбор не запускаем (ROBOTSTXT_OBEY=True)")
        if status != 200:
            verdict += f" (robots.txt: HTTP {status} — явных правил нет, действуют условия использования сайта)"

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "user_agent": UA,
        "robots_url": origin + "/robots.txt",
        "robots_status": status,
        "robots_text": text,
        "probes": rows,
        "verdict": verdict,
        "exit_code": code,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"robots.txt: {origin}/robots.txt → HTTP {status}")
    if text:
        print("---\n" + text.strip() + "\n---")
    for r in rows:
        print(f"  {r['url']}\n    urllib.robotparser={r['urllib_robotparser']}  "
              f"Protego(Scrapy)={r['protego_scrapy']}  → {'можно' if r['итог'] else 'НЕЛЬЗЯ'}")
    print("Вердикт:", verdict)
    return code


if __name__ == "__main__":
    sys.exit(main())
