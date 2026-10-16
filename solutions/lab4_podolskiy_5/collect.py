"""ЛР-4, вариант 5 — браузерный сбор (Playwright, sync API).

Сценарий: вход через форму с CSRF (один раз) → storage_state в auth_state.json →
пагинация ленты /page/N после входа (кликом «Next») → обход разделов тегов
/tag/<t>/page/N с той же сессией. Итоговый набор — «цитата в разделе тега».

Запуск:
    python collect.py                 # основной прогон (блокировка медиа/шрифтов/стилей включена)
    python collect.py --no-block      # без блокировки ресурсов
    python collect.py --trace         # trace.zip для этапа входа и ленты (artifacts/trace.zip)
    python collect.py --max-tags 5    # быстрый пробный прогон
    python collect.py --auth-check    # только проверить, что сессия из auth_state.json действует

Коды возврата: 0 — успех; 2 — не удалось войти / сессия потеряна; 3 — записей меньше
--min-records; 4 — smoke-check не пройден (только с --strict); 5 — слишком много сбоев.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import (BrowserContext, Error as PWError, Page, Response,
                                 TimeoutError as PWTimeout, expect, sync_playwright)

import common as C

BLOCK_TYPES = {"image", "media", "font", "stylesheet"}
NEXT_NAME = re.compile(r"^\s*Next\b")


# ------------------------------------------------------------------ контекст
def new_context(browser, storage_state: str | None = None, block: bool = True,
                stats: dict | None = None, js: bool = True) -> BrowserContext:
    ctx = browser.new_context(
        user_agent=C.USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        storage_state=storage_state,
        java_script_enabled=js,
    )
    ctx.set_default_timeout(C.ACTION_TIMEOUT_MS)
    ctx.set_default_navigation_timeout(C.NAV_TIMEOUT_MS)
    if stats is not None:
        stats.setdefault("requests", 0)
        stats.setdefault("blocked", 0)
        ctx.on("response", lambda r: stats.__setitem__("requests", stats["requests"] + 1))
    if block:
        def router(route):
            if route.request.resource_type in BLOCK_TYPES:
                if stats is not None:
                    stats["blocked"] += 1
                return route.abort()
            return route.continue_()
        ctx.route("**/*", router)
    return ctx


def attach_traffic_meter(ctx: BrowserContext, page: Page, stats: dict) -> None:
    """Трафик «по проводу» через CDP: Network.loadingFinished.encodedDataLength (Chromium)."""
    stats.setdefault("bytes", 0)
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.on("Network.loadingFinished",
           lambda e: stats.__setitem__("bytes", stats["bytes"] + int(e.get("encodedDataLength", 0))))


def save_artifacts(page: Page, tag: str) -> list[str]:
    """Скриншот + HTML при сбое (artifacts/<tag>_<время>.png|html)."""
    safe = re.sub(r"[^\w.-]+", "_", tag)[:80]
    stem = C.ARTIFACTS / f"{safe}_{time.strftime('%Y%m%d-%H%M%S')}"
    saved = []
    try:
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        saved.append(stem.with_suffix(".png").name)
    except PWError as e:
        print(f"  [artifacts] скриншот не сохранён: {str(e).splitlines()[0][:100]}")
    try:
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        saved.append(stem.with_suffix(".html").name)
    except PWError as e:
        print(f"  [artifacts] HTML не сохранён: {str(e).splitlines()[0][:100]}")
    print(f"  [artifacts] сохранено: {', '.join(saved) or '—'}")
    return saved


# ------------------------------------------------------------------ авторизация
def logout_link(page: Page):
    return page.get_by_role("link", name="Logout", exact=True)


def password_field(page: Page):
    """get_by_label("Password") с проверкой типа поля. На реальном сайте у метки пароля
    может стоять for="username" (тогда метка указывает на поле логина) — фолбэк по типу."""
    cand = page.get_by_label("Password", exact=True)
    if cand.count() == 1 and cand.get_attribute("type") == "password":
        return cand, 'get_by_label("Password")'
    return page.locator('input[type="password"]'), 'input[type="password"] (фолбэк: метка не указывает на поле пароля)'


def username_field(page: Page):
    cand = page.get_by_label("Username", exact=True)
    if cand.count() == 1 and cand.get_attribute("type") in ("text", "email", None):
        return cand, 'get_by_label("Username")'
    return page.locator('input[name="username"]'), 'input[name="username"] (фолбэк)'


def login(page: Page, ctx: BrowserContext, info: dict, save_state: bool = True) -> bool:
    """Вход через форму: CSRF-токен подставляет сама форма (hidden-поле).
    save_state=False — для диагностики и замеров (auth_state.json не перезаписывается)."""
    page.goto(f"{C.BASE_URL}/login", wait_until="domcontentloaded")
    submit = page.get_by_role("button", name="Login")
    expect(submit).to_be_visible()
    csrf = page.locator('input[name="csrf_token"]')
    info["csrf_fields"] = csrf.count()
    info["csrf_token_masked"] = C.mask(csrf.first.get_attribute("value") if csrf.count() else None, 4)
    user, info["username_locator"] = username_field(page)
    pwd, info["password_locator"] = password_field(page)
    user.fill(C.USERNAME)
    pwd.fill(C.PASSWORD)
    with page.expect_response(lambda r: r.request.method == "POST" and "/login" in r.url) as resp_info:
        submit.click()
    info["login_post_status"] = resp_info.value.status
    try:
        expect(logout_link(page)).to_be_visible(timeout=C.ACTION_TIMEOUT_MS)
    except AssertionError:
        save_artifacts(page, "login_failed")
        return False
    if save_state:
        ctx.storage_state(path=str(C.STATE_PATH))
        info["state_saved"] = str(C.STATE_PATH.name)
        print(f"[auth] вход выполнен как {C.mask(C.USERNAME, 2)}, CSRF {info['csrf_token_masked']}, "
              f"storage_state → {C.STATE_PATH.name}")
    return True


def session_valid(page: Page) -> bool:
    page.goto(f"{C.BASE_URL}/", wait_until="domcontentloaded")
    return logout_link(page).count() > 0


# ------------------------------------------------------------------ бэкенд для Crawler
class BrowserBackend:
    approach = "browser"

    def __init__(self, ctx: BrowserContext, page: Page, stats: dict, auth_info: dict,
                 quotes_wait_ms: int = C.QUOTES_WAIT_MS):
        self.ctx, self.page, self.stats, self.auth_info = ctx, page, stats, auth_info
        self.quotes_wait_ms = quotes_wait_ms
        self.extract_ms = 0.0
        self._mark = (0, 0)

    def _delta(self) -> tuple[int, int]:
        cur = (self.stats.get("requests", 0), self.stats.get("bytes", 0))
        d = (cur[0] - self._mark[0], cur[1] - self._mark[1])
        self._mark = cur
        return d

    def _after_navigation(self, resp: Response | None, url: str) -> C.PageResult:
        page = self.page
        status = resp.status if resp else 0
        if status >= 400:
            save_artifacts(page, f"http{status}_{url.replace(C.BASE_URL, '')}")
            raise C.PageError(f"http_{status}", url)
        try:
            page.wait_for_selector(C.QUOTE_SEL, state="attached", timeout=self.quotes_wait_ms)
        except PWTimeout as e:
            save_artifacts(page, f"timeout_{url.replace(C.BASE_URL, '')}")
            raise C.PageError("timeout", url, str(e).splitlines()[0][:150]) from e
        t0 = time.perf_counter()
        raws = page.eval_on_selector_all(C.QUOTE_SEL, C.JS_EXTRACT)
        self.extract_ms += (time.perf_counter() - t0) * 1000
        nxt = self._next_link()
        next_url = urljoin(page.url, nxt.get_attribute("href")) if nxt.count() else None
        req, byt = self._delta()
        return C.PageResult(url=page.url, page_no=C.page_no_of(page.url), raws=raws,
                            next_url=next_url, auth_ok=logout_link(page).count() > 0,
                            status=status, requests=req, bytes=byt)

    def _next_link(self):
        nav_link = self.page.get_by_role("navigation").get_by_role("link", name=NEXT_NAME)
        if nav_link.count():
            return nav_link.first
        return self.page.locator("li.next > a")          # фолбэк: класс компонента пагинации

    def load(self, url: str) -> C.PageResult:
        self._delta()
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded")
        except PWTimeout as e:
            save_artifacts(self.page, f"navtimeout_{url.replace(C.BASE_URL, '')}")
            raise C.PageError("nav_timeout", url, str(e).splitlines()[0][:150]) from e
        except PWError as e:
            raise C.PageError("nav_error", url, str(e).splitlines()[0][:150]) from e
        return self._after_navigation(resp, url)

    def follow_next(self, cur: C.PageResult) -> C.PageResult | None:
        link = self._next_link()
        if link.count() == 0:
            return None
        target = urljoin(self.page.url, link.get_attribute("href"))
        self._delta()
        try:
            # эталонное ожидание: ждём именно ответ на навигацию по клику
            with self.page.expect_response(
                    lambda r: r.request.is_navigation_request() and r.url.rstrip("/") == target.rstrip("/")
            ) as resp_info:
                link.click()
            resp = resp_info.value
            self.page.wait_for_url(lambda u: u.rstrip("/") == target.rstrip("/"),
                                   wait_until="domcontentloaded")
        except PWTimeout as e:
            save_artifacts(self.page, f"clicktimeout_{target.replace(C.BASE_URL, '')}")
            raise C.PageError("click_timeout", target, str(e).splitlines()[0][:150]) from e
        return self._after_navigation(resp, target)

    def reauth(self) -> bool:
        print("[auth] сессия не активна — повторный вход (однократно)")
        self.ctx.clear_cookies()
        return login(self.page, self.ctx, self.auth_info)


# ------------------------------------------------------------------ сценарий
def ensure_auth(browser, block: bool, stats: dict, auth_info: dict, force_login: bool = False):
    """Контекст с действующей сессией: storage_state из файла, иначе однократный вход."""
    t0 = time.perf_counter()
    reuse = C.STATE_PATH.exists() and not force_login
    ctx = new_context(browser, storage_state=str(C.STATE_PATH) if reuse else None,
                      block=block, stats=stats)
    page = ctx.new_page()
    attach_traffic_meter(ctx, page, stats)
    ok = False
    try:
        if reuse and session_valid(page):
            auth_info["mode"] = "reused_storage_state"
            ok = True
            print(f"[auth] сессия восстановлена из {C.STATE_PATH.name} — повторный вход не нужен")
        else:
            if reuse:
                print(f"[auth] {C.STATE_PATH.name} устарел — выполняем вход")
                ctx.clear_cookies()
            auth_info["mode"] = "fresh_login"
            ok = login(page, ctx, auth_info)
    except PWError as e:          # сеть/таймаут (TimeoutError — подкласс Error) → артефакты, код 2
        auth_info["error"] = str(e).splitlines()[0][:200]
        print(f"[auth] сбой при входе: {auth_info['error']}")
        save_artifacts(page, "auth_error")
        ok = False
    auth_info["auth_s"] = round(time.perf_counter() - t0, 3)
    return ctx, page, ok


def auth_check() -> int:
    """Проверка переиспользования storage_state: новый контекст из auth_state.json, вход не нужен."""
    stats: dict = {"requests": 0, "blocked": 0, "bytes": 0}
    info: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=C.HEADLESS, executable_path=C.CHROMIUM_PATH)
        try:
            ctx, page, ok = ensure_auth(browser, True, stats, info)
            ctx.close()
        finally:
            browser.close()
    print(f"[auth-check] режим: {info.get('mode')}, сессия активна: {ok}, {info.get('auth_s')} c, "
          f"запросов: {stats['requests']}")
    return 0 if ok else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-block", action="store_true", help="не блокировать изображения/шрифты/стили/медиа")
    ap.add_argument("--trace", action="store_true", help="записать trace.zip для входа и ленты")
    ap.add_argument("--force-login", action="store_true", help="игнорировать auth_state.json")
    ap.add_argument("--max-tags", type=int, default=None, help="ограничить число тегов (пробный прогон)")
    ap.add_argument("--min-records", type=int, default=200)
    ap.add_argument("--strict", action="store_true", help="код 4, если smoke-check не пройден")
    ap.add_argument("--auth-check", action="store_true",
                    help="только проверить/восстановить сессию из auth_state.json (без сбора и перезаписи данных)")
    args = ap.parse_args(argv)
    if args.auth_check:
        return auth_check()

    rid = C.run_id()
    log = C.CollectionLog("browser", rid)
    robots = C.RobotsGuard(C.BASE_URL)
    print(f"[run {rid}] BASE_URL={C.BASE_URL} | robots.txt: HTTP {robots.status} | "
          f"пауза {robots.pause(C.POLITE_PAUSE)} c | блокировка ресурсов: {not args.no_block}")
    stats: dict = {"requests": 0, "blocked": 0, "bytes": 0}
    auth_info: dict = {}
    metrics: dict = {"run_id": rid, "approach": "browser", "base_url": C.BASE_URL,
                     "started_at": C.now_iso(), "block_resources": not args.no_block}
    exit_code = 0
    crawler = None
    t0 = time.perf_counter()
    with C.PeakRSS() as mem, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=C.HEADLESS, executable_path=C.CHROMIUM_PATH)
        try:
            ctx, page, ok = ensure_auth(browser, not args.no_block, stats, auth_info, args.force_login)
            try:
                log.add(stage="auth", section="login", iteration=0, url=f"{C.BASE_URL}/login",
                        duration_s=auth_info.get("auth_s"), status="ok" if ok else "failed",
                        note=auth_info.get("mode", ""))
                if not ok:
                    print("[auth] ВХОД НЕ ВЫПОЛНЕН — см. artifacts/login_failed_* или artifacts/auth_error_*")
                    exit_code = 2
                else:
                    if args.trace:
                        ctx.tracing.start(screenshots=True, snapshots=True)
                    backend = BrowserBackend(ctx, page, stats, auth_info)
                    crawler = C.Crawler(backend=backend, log=log, robots=robots)

                    def stop_trace():
                        if args.trace:
                            ctx.tracing.stop(path=str(C.ARTIFACTS / "trace.zip"))
                            args.trace = False
                            print("[trace] artifacts/trace.zip (лента после входа)")

                    try:
                        crawler.run(max_tags=args.max_tags, after_feed=stop_trace)
                    except C.AuthLost as e:
                        print(f"[auth] {e}")
                        save_artifacts(page, "auth_lost")
                        exit_code = 2
                    except C.TooManyFailures as e:
                        print(f"[stop] превышен лимит сбоев: {e}")
                        exit_code = 5
                    metrics["extract_ms_total"] = round(backend.extract_ms, 1)
            finally:
                if args.trace:
                    ctx.tracing.stop(path=str(C.ARTIFACTS / "trace.zip"))
                ctx.close()
        finally:
            browser.close()
    duration = time.perf_counter() - t0
    log.flush()

    if crawler is not None:
        pairs, quotes = C.to_frames(crawler)
        pairs.to_csv(C.OUT_DIR / "data_browser.csv", index=False, encoding="utf-8")
        quotes.to_csv(C.OUT_DIR / "quotes_browser.csv", index=False, encoding="utf-8")
        q = C.quality_report(pairs)
        warns = C.smoke_check(pairs, name="data_browser.csv")
        metrics.update({
            "records": len(pairs), "feed_quotes": len(quotes), "raw_rows": crawler.raw_rows,
            "cycles": crawler.iteration, "failures": crawler.failures, "reauths": crawler.reauths,
            "stop_reasons": crawler.stop_reasons, "quality": q, "smoke_warnings": warns,
            "active_s": round(crawler.active_s, 2), "paused_s": round(crawler.paused_s, 2),
        })
        print(f"\nЗаписей (цитата × тег): {len(pairs)} | уникальных цитат в ленте: {len(quotes)} | "
              f"циклов: {crawler.iteration} | сбоев: {crawler.failures}")
        print(f"Дубликатов по ключу: {q['duplicates_by_key']} | доли пустых: {q['empty_share']}")
        if exit_code == 0 and len(pairs) < args.min_records:
            print(f"[итог] записей {len(pairs)} < {args.min_records}")
            exit_code = 3
        if exit_code == 0 and warns and args.strict:
            exit_code = 4
    metrics.update({
        "duration_s": round(duration, 2), "requests": stats["requests"], "blocked": stats["blocked"],
        "bytes": stats["bytes"], "mb": round(stats["bytes"] / 2**20, 3), "peak_rss_mb": mem.peak_mb,
        "auth": auth_info, "exit_code": exit_code, "finished_at": C.now_iso(),
    })
    if metrics.get("records"):
        metrics["sec_per_record"] = round(duration / metrics["records"], 4)
    C.save_json(C.METRICS / "browser.json", metrics)
    print(f"Время прогона: {duration:.1f} c | запросов: {stats['requests']} (заблокировано {stats['blocked']}) | "
          f"трафик: {metrics['mb']} МБ | пик памяти: {mem.peak_mb} МБ | код возврата: {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
