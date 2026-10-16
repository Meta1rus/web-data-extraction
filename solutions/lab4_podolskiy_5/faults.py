"""ЛР-4, вариант 5 — тест устойчивости подменой ответов (route.fulfill), задание повышенной сложности.

API в пайплайне варианта 5 нет (страницы рендерит сервер), поэтому подменяются ответы
на навигацию — HTML-страницы, которые разбирает сборщик:
  1. empty   — /page/3/ отдаёт 200 без карточек («No quotes found!»): ожидаем TimeoutError
               ожидания карточек → артефакты (png+html) → 1 повтор → раздел остановлен,
               собранное до сбоя (страницы 1–2) сохраняется, процесс не падает;
  2. http500 — первая страница первого тега отдаёт 500: ожидаем ошибку http_500 →
               артефакты → повтор → раздел пропущен, следующие теги собираются;
  3. expired — страница второго тега отдаётся «как анониму» (без Logout): ожидаем один
               повторный вход (предохранитель max_reauth=1), затем AuthLost (код 2 в collect.py).
Трассировка — отдельный короткий сценарий инцидента HTTP 500 (лента стр. 1 → тег с подменённым 500):
artifacts/trace_faults.zip, разбор шагов — в отчёте (раздел 9).
Результат: metrics/faults.json, журнал — metrics/faults_log.csv. Код возврата 0 — все проверки пройдены.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

import common as C
from collect import BrowserBackend, attach_traffic_meter, new_context
from bench import ensure_state

EMPTY_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Quotes to Scrape</title></head>
<body><div class="container"><div class="row header-box"><div class="col-md-4"><p><a href="/logout">Logout</a></p>
</div></div><div class="row"><div class="col-md-8">No quotes found!</div></div></div></body></html>"""


def run_case(browser, name: str, install, max_tags: int, quotes_wait_ms: int = 2000) -> dict:
    stats: dict = {"requests": 0, "blocked": 0, "bytes": 0}
    ctx = new_context(browser, storage_state=str(C.STATE_PATH), stats=stats)
    files_before = {p.name for p in C.ARTIFACTS.glob("*")}
    out: dict = {"case": name}
    try:
        page = ctx.new_page()
        attach_traffic_meter(ctx, page, stats)
        state = install(page) or {}
        backend = BrowserBackend(ctx, page, stats, {}, quotes_wait_ms=quotes_wait_ms)
        log = C.CollectionLog(f"faults:{name}", C.run_id(), path=C.METRICS / "faults_log.csv")
        crawler = C.Crawler(backend=backend, log=log, robots=C.RobotsGuard(C.BASE_URL),
                            limits=C.Limits(max_failures=10), pause=min(C.POLITE_PAUSE, 0.5))
        try:
            crawler.run(max_tags=max_tags)
            out["exception"] = None
        except C.AuthLost as e:
            out["exception"] = f"AuthLost: {e}"
        log.flush()
        out.update({"feed_quotes": len(crawler.quotes), "pairs": len(crawler.pairs),
                    "failures": crawler.failures, "reauths": crawler.reauths,
                    "stop_reasons": dict(crawler.stop_reasons), "cycles": crawler.iteration})
    finally:
        ctx.close()
    out["target_tag"] = state.get("tag")
    out["new_artifacts"] = sorted({p.name for p in C.ARTIFACTS.glob("*")} - files_before)
    return out


def main() -> int:
    results, checks = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=C.HEADLESS, executable_path=C.CHROMIUM_PATH)
        try:
            ensure_state(browser)

            # 1. пустой ответ на /page/3/
            def empty(page):
                page.route("**/page/3/", lambda r: r.fulfill(status=200, content_type="text/html", body=EMPTY_HTML))
                return {"tag": None}
            r1 = run_case(browser, "empty", empty, max_tags=0)
            results.append(r1)
            checks.append(("empty: раздел остановлен с ошибкой ожидания, страницы 1–2 сохранены",
                           r1["stop_reasons"].get("feed", "").startswith("error:") and r1["feed_quotes"] == 20
                           and any(n.endswith(".png") for n in r1["new_artifacts"])))

            # 2. HTTP 500 на первой странице первого запрошенного тега
            def http500(page):
                state = {"tag": None}

                def handler(route):
                    tag = route.request.url.split("/tag/", 1)[1].split("/", 1)[0]
                    state["tag"] = state["tag"] or tag
                    if tag != state["tag"]:
                        return route.continue_()
                    return route.fulfill(status=500, content_type="text/html",
                                         body="<h1>Internal Server Error</h1>")
                page.route("**/tag/**", handler)
                return state
            r2 = run_case(browser, "http500", http500, max_tags=3)
            results.append(r2)
            t1 = r2.get("target_tag")
            others = [k for k in r2["stop_reasons"] if k not in ("feed", t1, "__run__")]
            checks.append(("http500: тег пропущен после повтора, следующие теги собраны",
                           r2["stop_reasons"].get(t1) == "error:http_500" and r2["failures"] == 2
                           and others and all(r2["stop_reasons"][k] in ("no_next", "stall") for k in others)
                           and r2["pairs"] > 0))

            # 3. «истёкшая сессия»: первая страница первого тега отдаётся без Logout
            def expired(page):
                state = {"tag": None}

                def handler(route):
                    tag = route.request.url.split("/tag/", 1)[1].split("/", 1)[0]
                    state["tag"] = state["tag"] or tag
                    if tag != state["tag"]:
                        return route.continue_()
                    resp = route.fetch()
                    body = resp.text().replace(">Logout<", ">Login<").replace('href="/logout"', 'href="/login"')
                    return route.fulfill(response=resp, body=body)
                page.route("**/tag/**", handler)
                return state
            r3 = run_case(browser, "expired", expired, max_tags=3)
            results.append(r3)
            checks.append(("expired: ровно один повторный вход, затем AuthLost",
                           r3["reauths"] == 1 and (r3["exception"] or "").startswith("AuthLost")))

            # трасса для разбора инцидента в Trace Viewer (сценарий 2)
            _trace_incident(browser, sorted(_tags_of_feed(browser))[0])
        finally:
            browser.close()

    C.save_json(C.METRICS / "faults.json", {"generated_at": C.now_iso(), "results": results,
                                            "checks": [{"check": c, "passed": bool(p)} for c, p in checks]})
    for c, p in checks:
        print(("PASS " if p else "FAIL ") + c)
    print("Сохранено: metrics/faults.json, metrics/faults_log.csv, artifacts/trace_faults.zip")
    return 0 if all(p for _, p in checks) else 7


def _tags_of_feed(browser) -> set[str]:
    ctx = new_context(browser, storage_state=str(C.STATE_PATH))
    try:
        page = ctx.new_page()
        page.goto(f"{C.BASE_URL}/page/1/", wait_until="domcontentloaded")
        raws = page.eval_on_selector_all(C.QUOTE_SEL, C.JS_EXTRACT)
        return set(C.tag_links_of(raws, page.url))
    finally:
        ctx.close()


def _trace_incident(browser, tag: str) -> None:
    """Короткий сценарий с трассировкой: лента стр.1 → тег с подменённым 500 → артефакты."""
    stats: dict = {}
    ctx = new_context(browser, storage_state=str(C.STATE_PATH), stats=stats)
    ctx.tracing.start(screenshots=True, snapshots=True)
    try:
        page = ctx.new_page()
        page.route(f"**/tag/{tag}/**", lambda r: r.fulfill(status=500, content_type="text/html",
                                                           body="<h1>Internal Server Error</h1>"))
        be = BrowserBackend(ctx, page, stats, {}, quotes_wait_ms=2000)
        be.load(f"{C.BASE_URL}/page/1/")
        try:
            be.load(f"{C.BASE_URL}/tag/{tag}/page/1/")
        except C.PageError as e:
            print(f"[trace] инцидент зафиксирован: {e.kind} {e.url}")
    finally:
        ctx.tracing.stop(path=str(C.ARTIFACTS / "trace_faults.zip"))
        ctx.close()


if __name__ == "__main__":
    sys.exit(main())
