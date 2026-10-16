"""ЛР-4, вариант 5 — замеры оптимизаций браузерного режима (этап 5).

  A. Блокировка ресурсов (image/media/font/stylesheet) через route: лента /page/1..N после входа,
     режимы «всё грузим» / «блокируем», по --reps повторов, порядок режимов чередуется.
  B. Массовое извлечение eval_on_selector_all против поэлементного перебора локаторов.
  C. Переиспользование storage_state против нового входа формой.

Паузы вежливости в замеры времени не входят (считаются отдельно).
Результат: metrics/bench_block.csv, bench_extract.csv, bench_auth.csv, bench.json.
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
import time

import pandas as pd
from playwright.sync_api import sync_playwright

import common as C
from collect import (BrowserBackend, attach_traffic_meter, login, logout_link, new_context,
                     session_valid)


def ensure_state(browser) -> None:
    if C.STATE_PATH.exists():
        ctx = new_context(browser, storage_state=str(C.STATE_PATH))
        try:
            if session_valid(ctx.new_page()):
                return
        finally:
            ctx.close()
    ctx = new_context(browser)
    try:
        if not login(ctx.new_page(), ctx, {}):
            raise SystemExit("вход не выполнен — замеры невозможны")
    finally:
        ctx.close()


def crawl_feed(browser, block: bool, pages: int, pause: float) -> dict:
    stats: dict = {"requests": 0, "blocked": 0, "bytes": 0}
    ctx = new_context(browser, storage_state=str(C.STATE_PATH), block=block, stats=stats)
    try:
        page = ctx.new_page()
        attach_traffic_meter(ctx, page, stats)
        be = BrowserBackend(ctx, page, stats, {})
        t0 = time.perf_counter()
        res = be.load(f"{C.BASE_URL}/page/1/")
        n, records = 1, len(res.raws)
        active = time.perf_counter() - t0
        while res.next_url and n < pages:          # предохранитель: не больше `pages` страниц
            time.sleep(pause)                      # вежливость, в замер не входит
            t1 = time.perf_counter()
            res = be.follow_next(res)
            active += time.perf_counter() - t1
            if res is None:
                break
            n += 1
            records += len(res.raws)
        return {"mode": "block" if block else "full", "pages": n, "records": records,
                "seconds": round(active, 3), "requests": stats["requests"], "blocked": stats["blocked"],
                "kb": round(stats["bytes"] / 1024, 1)}
    finally:
        ctx.close()


PER_ELEMENT_FIELDS = [('[itemprop="text"]', "text"), ('[itemprop="author"]', "author")]


def extract_per_element(page) -> list[dict]:
    out = []
    for q in page.locator(C.QUOTE_SEL).all():
        about = q.locator('a[href^="/author/"]')
        gr = q.locator('a[href*="goodreads.com"]')
        kw = q.locator('meta[itemprop="keywords"]')
        out.append({
            "text": q.locator('[itemprop="text"]').text_content(),
            "author": q.locator('[itemprop="author"]').text_content(),
            "author_href": about.first.get_attribute("href") if about.count() else None,
            "goodreads_href": gr.first.get_attribute("href") if gr.count() else None,
            "keywords": kw.first.get_attribute("content") if kw.count() else None,
            "tag_links": [{"name": t.text_content(), "href": t.get_attribute("href")}
                          for t in q.locator("a.tag").all()],
        })
    return out


def bench_extract(browser, pages: int, pause: float) -> list[dict]:
    rows = []
    ctx = new_context(browser, storage_state=str(C.STATE_PATH))
    try:
        page = ctx.new_page()
        for n in range(1, pages + 1):
            if n > 1:
                time.sleep(pause)
            page.goto(f"{C.BASE_URL}/page/{n}/", wait_until="domcontentloaded")
            page.wait_for_selector(C.QUOTE_SEL, state="attached")
            t0 = time.perf_counter()
            bulk = page.eval_on_selector_all(C.QUOTE_SEL, C.JS_EXTRACT)
            t_bulk = time.perf_counter() - t0
            t0 = time.perf_counter()
            slow = extract_per_element(page)
            t_slow = time.perf_counter() - t0
            same = [C.quote_id(a["text"], a["author"]) for a in bulk] == \
                   [C.quote_id(b["text"], b["author"]) for b in slow]
            rows.append({"page": n, "records": len(bulk), "bulk_ms": round(t_bulk * 1000, 2),
                         "per_element_ms": round(t_slow * 1000, 2), "same_records": same})
    finally:
        ctx.close()
    return rows


def bench_auth(browser, reps: int, pause: float) -> list[dict]:
    rows = []
    for i in range(reps):
        for mode in (("restore", "fresh") if i % 2 == 0 else ("fresh", "restore")):
            time.sleep(pause)
            stats: dict = {"requests": 0, "blocked": 0}
            state = str(C.STATE_PATH) if mode == "restore" else None
            ctx = new_context(browser, storage_state=state, stats=stats)
            try:
                page = ctx.new_page()
                t0 = time.perf_counter()
                if mode == "restore":
                    ok = session_valid(page)
                else:
                    ok = login(page, ctx, {}, save_state=False) and logout_link(page).count() > 0
                rows.append({"rep": i + 1, "mode": mode, "ok": ok,
                             "seconds": round(time.perf_counter() - t0, 3), "requests": stats["requests"]})
            finally:
                ctx.close()
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--pages", type=int, default=10)
    args = ap.parse_args(argv)
    pause = C.POLITE_PAUSE
    summary: dict = {"generated_at": C.now_iso(), "base_url": C.BASE_URL, "reps": args.reps, "pages": args.pages}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=C.HEADLESS, executable_path=C.CHROMIUM_PATH)
        try:
            ensure_state(browser)
            # A. блокировка ресурсов
            block_rows = []
            for i in range(args.reps):
                for block in ((False, True) if i % 2 == 0 else (True, False)):
                    r = crawl_feed(browser, block, args.pages, pause)
                    r["rep"] = i + 1
                    block_rows.append(r)
                    print(f"[A] повтор {i + 1} {r['mode']:<5}: {r['pages']} стр., {r['seconds']} c, "
                          f"запросов {r['requests']}, заблокировано {r['blocked']}, {r['kb']} КБ")
            # B. массовое извлечение
            ext_rows = bench_extract(browser, args.pages, pause)
            # C. storage_state против входа
            auth_rows = bench_auth(browser, args.reps, pause)
        finally:
            browser.close()

    b = pd.DataFrame(block_rows)
    e = pd.DataFrame(ext_rows)
    a = pd.DataFrame(auth_rows)
    b.to_csv(C.METRICS / "bench_block.csv", index=False)
    e.to_csv(C.METRICS / "bench_extract.csv", index=False)
    a.to_csv(C.METRICS / "bench_auth.csv", index=False)

    med = b.groupby("mode")[["seconds", "requests", "blocked", "kb"]].median()
    summary["block"] = med.to_dict(orient="index")
    if {"full", "block"} <= set(med.index):
        summary["block"]["time_saving_pct"] = round(100 * (1 - med.loc["block", "seconds"] / med.loc["full", "seconds"]), 1)
        summary["block"]["traffic_saving_pct"] = round(100 * (1 - med.loc["block", "kb"] / med.loc["full", "kb"]), 1)
    summary["extract"] = {"bulk_ms_total": round(e["bulk_ms"].sum(), 1),
                          "per_element_ms_total": round(e["per_element_ms"].sum(), 1),
                          "speedup_x": round(e["per_element_ms"].sum() / max(e["bulk_ms"].sum(), 1e-9), 1),
                          "all_pages_same_records": bool(e["same_records"].all())}
    summary["auth"] = {m: {"median_s": round(st.median(g["seconds"]), 3), "median_requests": int(st.median(g["requests"])),
                           "all_ok": bool(g["ok"].all())} for m, g in a.groupby("mode")}
    C.save_json(C.METRICS / "bench.json", summary)

    print("\nA. Блокировка ресурсов (медианы):\n", med.to_string())
    print(f"   экономия времени {summary['block'].get('time_saving_pct')} %, трафика {summary['block'].get('traffic_saving_pct')} %")
    print(f"B. Извлечение: eval_on_selector_all {summary['extract']['bulk_ms_total']} мс против поэлементно "
          f"{summary['extract']['per_element_ms_total']} мс (×{summary['extract']['speedup_x']}), "
          f"записи совпадают: {summary['extract']['all_pages_same_records']}")
    print(f"C. Авторизация: {summary['auth']}")
    print("Сохранено: metrics/bench_*.csv, metrics/bench.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
