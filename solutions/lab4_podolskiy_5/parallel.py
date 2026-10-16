"""ЛР-4, вариант 5 — параллельный сбор разделов тегов (задание повышенной сложности).

async_playwright: один браузер, K независимых контекстов из одного storage_state (без повторного
входа), очередь тегов, ограничение одновременности K (по одному воркеру на контекст),
пауза вежливости — на каждого воркера. Для честного сравнения тот же код запускается
с K=1 и K=--workers на одном и том же подмножестве тегов; проверяются ошибки и совпадение наборов.

Запуск:  python parallel.py --workers 3 --max-tags 30
Результат: metrics/parallel.json. Код возврата 0 — ошибок нет и наборы совпали.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from urllib.parse import quote, urljoin

import pandas as pd
from playwright.async_api import Error as PWError, TimeoutError as PWTimeout, async_playwright

import common as C

MAX_PAGES_PER_TAG = 30


async def crawl_tag(page, tag: str, url: str, pause: float) -> tuple[set, int]:
    keys, pages = set(), 0
    for _ in range(MAX_PAGES_PER_TAG):                     # предохранитель по числу страниц
        resp = await page.goto(url, wait_until="domcontentloaded")
        if resp is None or resp.status >= 400:
            raise C.PageError(f"http_{resp.status if resp else 0}", url)
        await page.wait_for_selector(C.QUOTE_SEL, state="attached", timeout=C.QUOTES_WAIT_MS)
        if await page.get_by_role("link", name="Logout", exact=True).count() == 0:
            raise C.PageError("no_auth", url)
        raws = await page.eval_on_selector_all(C.QUOTE_SEL, C.JS_EXTRACT)
        pages += 1
        for r in raws:
            keys.add(C.pair_key(tag, C.quote_id(C.norm_text(r["text"]), C.norm_text(r["author"]))))
        nxt = page.locator("li.next > a")
        if await nxt.count() == 0:
            break
        url = urljoin(page.url, await nxt.first.get_attribute("href"))
        await asyncio.sleep(pause)                         # пауза вежливости, не ожидание данных
    return keys, pages


async def run(workers: int, tags: dict[str, str], pause: float) -> dict:
    queue: asyncio.Queue = asyncio.Queue()
    for t, u in tags.items():
        queue.put_nowait((t, u))
    keys: set = set()
    errors: list[dict] = []
    pages_total = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=C.HEADLESS, executable_path=C.CHROMIUM_PATH)
        try:
            async def worker(wid: int):
                nonlocal pages_total
                ctx = await browser.new_context(user_agent=C.USER_AGENT, locale="en-US",
                                                viewport={"width": 1280, "height": 900},
                                                storage_state=str(C.STATE_PATH))
                ctx.set_default_timeout(C.ACTION_TIMEOUT_MS)
                ctx.set_default_navigation_timeout(C.NAV_TIMEOUT_MS)
                await ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in
                                {"image", "media", "font", "stylesheet"} else r.continue_())
                page = await ctx.new_page()
                try:
                    for _ in range(len(tags)):              # предохранитель: не больше числа тегов
                        try:
                            tag, url = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        try:
                            k, p = await crawl_tag(page, tag, url, pause)
                            keys.update(k)
                            pages_total += p
                        except (PWTimeout, PWError, C.PageError) as e:
                            errors.append({"worker": wid, "tag": tag, "error": str(e).splitlines()[0][:150]})
                        await asyncio.sleep(pause)
                finally:
                    await ctx.close()

            t0 = time.perf_counter()
            await asyncio.gather(*(worker(i) for i in range(workers)))
            seconds = time.perf_counter() - t0
        finally:
            await browser.close()
    return {"workers": workers, "seconds": round(seconds, 2), "records": len(keys), "pages": pages_total,
            "errors": errors, "keys": keys}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-tags", type=int, default=30)
    args = ap.parse_args(argv)
    src = C.OUT_DIR / "quotes_browser.csv"
    if not src.exists() or not C.STATE_PATH.exists():
        print("Нужны quotes_browser.csv и auth_state.json — сначала запустите collect.py")
        return 2
    feed = C.typed(pd.read_csv(src, keep_default_na=False))
    tag_names = sorted({t for s in feed["tags_all"] for t in str(s).split(",") if t})[: args.max_tags]
    tags = {t: f"{C.BASE_URL}/tag/{quote(t)}/page/1/" for t in tag_names}
    pause = C.POLITE_PAUSE

    seq = asyncio.run(run(1, tags, pause))
    par = asyncio.run(run(args.workers, tags, pause))
    same = seq["keys"] == par["keys"]
    ref_same = None
    ref = C.OUT_DIR / "data_browser.csv"
    if ref.exists():
        db = pd.read_csv(ref, keep_default_na=False)
        ref_keys = set(db.loc[db["tag"].isin(tag_names), "record_key"])
        ref_same = ref_keys == par["keys"]
    out = {"generated_at": C.now_iso(), "tags": len(tags), "pause_s": pause,
           "sequential": {k: v for k, v in seq.items() if k != "keys"},
           "parallel": {k: v for k, v in par.items() if k != "keys"},
           "speedup_x": round(seq["seconds"] / max(par["seconds"], 1e-9), 2),
           "same_records_seq_vs_par": same, "same_as_data_browser": ref_same}
    C.save_json(C.METRICS / "parallel.json", out)
    print(f"Тегов: {len(tags)} | K=1: {seq['seconds']} c, {seq['records']} записей, ошибок {len(seq['errors'])} | "
          f"K={args.workers}: {par['seconds']} c, {par['records']} записей, ошибок {len(par['errors'])} | "
          f"ускорение ×{out['speedup_x']} | наборы совпадают: {same} | с data_browser.csv: {ref_same}")
    return 0 if same and not seq["errors"] and not par["errors"] else 8


if __name__ == "__main__":
    sys.exit(main())
