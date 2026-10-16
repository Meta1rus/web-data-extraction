"""ЛР-4, вариант 5 — протокол диагностики источника (этап 1).

Проверки (каждая — измерение, а не предположение):
  1. robots.txt;
  2. requests → /page/1 без входа: размер HTML, число карточек, ссылки «(Goodreads page)»,
     где лежит целевой текст (<span itemprop=text> / <script> / JSON-LD / __NEXT_DATA__);
  3. requests → /login: форма, поля, hidden csrf_token, cookie сессии;
  4. POST /login без csrf_token — пускает ли (проверка обязательности CSRF);
  5. вход requests.Session с csrf_token → что появилось после входа;
  6. /api/quotes?page=1 (XHR-эндпоинт соседнего раздела /scroll) без входа;
  7. Playwright: каталог сетевых запросов при входе и переходе по страницам
     (аналог DevTools → Network, фильтр Fetch/XHR);
  8. Playwright с отключённым JavaScript: работает ли вход и пагинация.
Итог — metrics/diagnostics.json и таблица протокола в консоли.
"""
from __future__ import annotations

import re
import sys
import time
from collections import Counter
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

import common as C
from collect import login, new_context


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def page_facts(html: str) -> dict:
    s = soup_of(html)
    cards = C.extract_quotes_bs(s)
    scripts = s.find_all("script")
    first = cards[0]["text"] if cards else ""
    where = None
    if first:
        needle = C.norm_text(first)[:40]
        for node in s.find_all(string=lambda t: t and needle in C.norm_text(t)):
            parent = node.parent
            where = f"<{parent.name} " + " ".join(
                f'{k}="{" ".join(v) if isinstance(v, list) else v}"' for k, v in parent.attrs.items()) + ">"
            break
    return {
        "bytes": len(html.encode("utf-8")),
        "quote_cards": len(cards),
        "goodreads_links": sum(1 for c in cards if c["goodreads_href"]),
        "login_link": bool(s.find("a", string=re.compile(r"^\s*Login\s*$"))),
        "logout_link": bool(s.find("a", string=re.compile(r"^\s*Logout\s*$"))),
        "script_tags": len(scripts),
        "json_ld": bool(s.select('script[type="application/ld+json"]')),
        "next_data": "__NEXT_DATA__" in html,
        "var_data_json": bool(re.search(r"var\s+data\s*=\s*\[", html)),
        "first_quote_found_in": where,
        "first_quote_sample": first[:60],
    }


def main() -> int:
    d: dict = {"base_url": C.BASE_URL, "generated_at": C.now_iso(), "user_agent": C.USER_AGENT}
    H = {"User-Agent": C.USER_AGENT}
    pause = C.POLITE_PAUSE

    # 1. robots.txt
    robots = C.RobotsGuard(C.BASE_URL)
    d["robots_txt_status"] = robots.status
    d["robots_allows_targets"] = {p: robots.allowed(f"{C.BASE_URL}{p}") for p in ("/login", "/page/1/", "/tag/love/page/1/")}

    # 2. /page/1 без входа
    t0 = time.perf_counter()
    r = requests.get(f"{C.BASE_URL}/page/1/", headers=H, timeout=C.HTTP_TIMEOUT)
    d["anon_page1"] = {"status": r.status_code, "seconds": round(time.perf_counter() - t0, 3), **page_facts(r.text)}
    time.sleep(pause)

    # 3. /login
    s_nocsrf = requests.Session()
    s_nocsrf.headers.update(H)
    r = s_nocsrf.get(f"{C.BASE_URL}/login", timeout=C.HTTP_TIMEOUT)
    s = soup_of(r.text)
    form = s.select_one("form")
    token = s.select_one('input[name="csrf_token"]')
    d["login_form"] = {
        "status": r.status_code, "bytes": len(r.content),
        "action": form.get("action") if form else None, "method": form.get("method") if form else None,
        "inputs": [{"name": i.get("name"), "type": i.get("type"), "id": i.get("id")} for i in s.select("form input")],
        "labels": [{"text": l.get_text(strip=True), "for": l.get("for")} for l in s.select("form label")],
        "csrf_token_len": len(token.get("value", "")) if token else 0,
        "csrf_token_masked": C.mask(token.get("value") if token else None, 4),
        "set_cookie_names": sorted({c.name for c in s_nocsrf.cookies}),
    }
    time.sleep(pause)

    # 4. POST без csrf_token (cookie сессии есть, токена нет)
    r = s_nocsrf.post(f"{C.BASE_URL}/login", data={"username": C.USERNAME, "password": C.PASSWORD},
                      timeout=C.HTTP_TIMEOUT)
    d["post_without_csrf"] = {"status_chain": [h.status_code for h in r.history] + [r.status_code],
                              "final_path": urlsplit(r.url).path,
                              "logged_in": page_facts(r.text)["logout_link"]}
    s_nocsrf.close()
    time.sleep(pause)

    # 5. Вход с csrf_token
    sess = requests.Session()
    sess.headers.update(H)
    r = sess.get(f"{C.BASE_URL}/login", timeout=C.HTTP_TIMEOUT)
    tok = soup_of(r.text).select_one('input[name="csrf_token"]')
    time.sleep(pause)
    r = sess.post(f"{C.BASE_URL}/login", timeout=C.HTTP_TIMEOUT,
                  data={"csrf_token": tok.get("value") if tok else "", "username": C.USERNAME, "password": C.PASSWORD})
    d["post_with_csrf"] = {"status_chain": [h.status_code for h in r.history] + [r.status_code],
                           "final_path": urlsplit(r.url).path, "logged_in": page_facts(r.text)["logout_link"]}
    time.sleep(pause)
    r = sess.get(f"{C.BASE_URL}/page/1/", timeout=C.HTTP_TIMEOUT)
    d["auth_page1"] = {"status": r.status_code, **page_facts(r.text)}
    sess.close()
    time.sleep(pause)

    # 6. XHR-эндпоинт соседнего раздела /scroll — без входа
    try:
        r = requests.get(f"{C.BASE_URL}/api/quotes", params={"page": 1}, headers=H, timeout=C.HTTP_TIMEOUT)
        body = r.json() if "json" in r.headers.get("Content-Type", "") else {}
        q0 = (body.get("quotes") or [{}])[0]
        d["api_quotes_anon"] = {"status": r.status_code, "content_type": r.headers.get("Content-Type"),
                                "keys": sorted(body), "quotes": len(body.get("quotes") or []),
                                "author_keys": sorted((q0.get("author") or {}).keys()),
                                "goodreads_link_present": bool((q0.get("author") or {}).get("goodreads_link"))}
    except (requests.RequestException, ValueError) as e:
        d["api_quotes_anon"] = {"error": f"{type(e).__name__}: {e}"}
    time.sleep(pause)

    # 7–8. Playwright: каталог запросов и режим без JS
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=C.HEADLESS, executable_path=C.CHROMIUM_PATH)
        try:
            # 7. Network: вход + переход на /page/2 кликом
            ctx = new_context(browser, block=False)
            try:
                page = ctx.new_page()
                seen: list[dict] = []
                page.on("response", lambda resp: seen.append({
                    "method": resp.request.method, "type": resp.request.resource_type,
                    "status": resp.status, "path": urlsplit(resp.url).path or "/",
                    "host": urlsplit(resp.url).hostname}))
                info: dict = {}
                ok = login(page, ctx, info, save_state=False)
                page.goto(f"{C.BASE_URL}/page/1/", wait_until="load")
                nxt = page.get_by_role("navigation").get_by_role("link", name=re.compile(r"^\s*Next\b"))
                with page.expect_response(lambda x: x.request.is_navigation_request()):
                    nxt.first.click()
                page.wait_for_load_state("load")
                d["network"] = {
                    "logged_in": ok, "login_locators": info,
                    "by_type": dict(Counter(x["type"] for x in seen)),
                    "xhr_fetch": [x for x in seen if x["type"] in ("xhr", "fetch")],
                    "documents": [x for x in seen if x["type"] == "document"],
                    "other_hosts": sorted({x["host"] for x in seen if x["host"] != urlsplit(C.BASE_URL).hostname}),
                    "total": len(seen),
                }
            finally:
                ctx.close()
            # 8. JavaScript отключён
            ctx = new_context(browser, block=False, js=False)
            try:
                page = ctx.new_page()
                info2: dict = {}
                ok2 = login(page, ctx, info2, save_state=False)
                page.goto(f"{C.BASE_URL}/page/1/", wait_until="domcontentloaded")
                # считаем локаторами: движок Playwright работает и при отключённом JS страницы
                cards = page.locator(C.QUOTE_SEL).count()
                gr = page.locator(C.QUOTE_SEL).locator('a[href*="goodreads.com"]').count()
                has_next = page.locator("li.next > a").count() > 0
                d["js_disabled"] = {"login_ok": ok2, "quote_cards": cards, "goodreads_links": gr,
                                    "next_link": has_next}
            finally:
                ctx.close()
        finally:
            browser.close()

    # Вывод протокола
    a, b = d["anon_page1"], d["auth_page1"]
    xhr = len(d["network"]["xhr_fetch"])
    no_js_ok = d["js_disabled"]["login_ok"] and d["js_disabled"]["quote_cards"] > 0
    csrf_needed = not d["post_without_csrf"]["logged_in"] and d["post_with_csrf"]["logged_in"]
    browser_needed = not (a["quote_cards"] > 0 and xhr == 0 and no_js_ok)
    d["conclusion"] = {
        "data_in_server_html": a["quote_cards"] > 0,
        "visible_only_after_login": ["goodreads_url"] if (a["goodreads_links"] == 0 and b["goodreads_links"] > 0) else [],
        "xhr_on_target_pages": xhr,
        "works_without_js": no_js_ok,
        "csrf_required": csrf_needed,
        "browser_required": browser_needed,
        "text": (
            "Браузер технически не обязателен: страницы рендерятся сервером, XHR на целевых страницах нет, "
            "без JS вход и пагинация работают. Барьер — авторизация (форма с CSRF + cookie сессии); "
            "её воспроизводит requests.Session. Браузерная реализация выполнена как требование ЛР и как база сравнения."
            if not browser_needed else
            "Есть признаки клиентского рендеринга или XHR — браузер (или найденный эндпоинт) необходим; см. детали."),
    }
    C.save_json(C.METRICS / "diagnostics.json", d)
    rows = [
        ("Ответ сервера (requests, без входа)", f"HTTP {d['anon_page1']['status']}, {a['bytes']} байт, карточек {a['quote_cards']}, «Goodreads» {a['goodreads_links']}"),
        ("Ответ сервера (requests, после входа)", f"{b['bytes']} байт, карточек {b['quote_cards']}, «Goodreads» {b['goodreads_links']}, Logout={b['logout_link']}"),
        ("Целевой текст в исходном HTML", f"найден в {a['first_quote_found_in']}; <script>: {a['script_tags']}, JSON-LD: {a['json_ld']}, __NEXT_DATA__: {a['next_data']}, var data: {a['var_data_json']}"),
        ("Форма /login", f"{d['login_form']['method']} {d['login_form']['action']}, поля {[i['name'] or i['type'] for i in d['login_form']['inputs']]}, csrf_token {d['login_form']['csrf_token_len']} симв., cookie {d['login_form']['set_cookie_names']}"),
        ("POST без csrf_token / с csrf_token", f"{d['post_without_csrf']['status_chain']} вход={d['post_without_csrf']['logged_in']} / {d['post_with_csrf']['status_chain']} вход={d['post_with_csrf']['logged_in']}"),
        ("Network → Fetch/XHR (вход + пагинация)", f"всего ответов {d['network']['total']}, по типам {d['network']['by_type']}, XHR/fetch: {xhr}"),
        ("/api/quotes?page=1 без входа (раздел /scroll)", str(d["api_quotes_anon"])),
        ("Сайт с отключённым JS", str(d["js_disabled"])),
        ("robots.txt", f"HTTP {d['robots_txt_status']}"),
        ("Вывод", d["conclusion"]["text"]),
    ]
    print("| Проверка | Результат |\n|---|---|")
    for k, v in rows:
        print(f"| {k} | {v} |")
    print("\nСохранено: metrics/diagnostics.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
