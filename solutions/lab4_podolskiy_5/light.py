"""ЛР-4, вариант 5 — дешёвая альтернатива без браузера (requests.Session).

Вход: GET /login → извлечь hidden csrf_token → POST /login (form-urlencoded) с той же
cookie-сессией → проверка «Logout». Cookies сессии сохраняются в auth_state_light.json
и переиспользуются в следующих запусках (повторный вход только при истёкшей сессии).
Дальше — тот же обход, что у браузера (общий Crawler из common.py): лента /page/N и
разделы тегов /tag/<t>/page/N, разбор BeautifulSoup теми же селекторами.

Запуск:
    python light.py                  # основной прогон
    python light.py --force-login    # игнорировать сохранённые cookies (замер «с входом»)
Коды возврата — как у collect.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import common as C

RETRIES = 3


class HttpMeter:
    """Счётчик запросов и трафика «по проводу»: тело — сколько байт прочитано из сокета
    (до распаковки gzip), заголовки — по длине строк ответа."""

    def __init__(self):
        self.requests = 0
        self.bytes = 0

    def account(self, resp: requests.Response) -> None:
        for r in [*resp.history, resp]:
            self.requests += 1
            body = r.raw.tell() if r.raw is not None and hasattr(r.raw, "tell") else 0
            if not body:
                body = len(r.content or b"")
            headers = sum(len(k) + len(v) + 4 for k, v in r.headers.items()) + 17
            self.bytes += body + headers


class LightClient:
    def __init__(self, meter: HttpMeter):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": C.USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})
        self.meter = meter

    def request(self, method: str, url: str, **kw) -> requests.Response:
        """Шаблон занятия 3: таймаут, 3 попытки, экспоненциальная пауза, 5xx — повтор."""
        last: Exception | None = None
        for attempt in range(1, RETRIES + 1):
            try:
                r = self.s.request(method, url, timeout=C.HTTP_TIMEOUT, **kw)
                _ = r.content
                self.meter.account(r)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                return r
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                last = e
                if attempt < RETRIES:
                    time.sleep(2 ** (attempt - 1))
        raise C.PageError("http_failed", url, f"{type(last).__name__}: {last}")

    # --- авторизация
    @staticmethod
    def is_logged_in(html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        return any(a.get_text(strip=True) == "Logout" for a in soup.select("a[href]"))

    def login(self, info: dict) -> bool:
        r = self.request("GET", f"{C.BASE_URL}/login")
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.select_one('form[action$="/login"]') or soup.select_one("form")
        token_input = form.select_one('input[name="csrf_token"]') if form else None
        token = token_input.get("value") if token_input else None
        info["csrf_token_masked"] = C.mask(token, 4)
        if not token:
            print("[auth] csrf_token не найден в форме входа")
            return False
        # все hidden-поля формы + логин/пароль (на случай появления новых скрытых полей)
        data = {i.get("name"): i.get("value", "") for i in form.select('input[type="hidden"][name]')}
        data.update({"username": C.USERNAME, "password": C.PASSWORD})
        action = urljoin(r.url, form.get("action") or "/login")
        resp = self.request("POST", action, data=data)
        info["login_post_chain"] = [h.status_code for h in resp.history] + [resp.status_code]
        ok = self.is_logged_in(resp.text)
        if ok:
            self.save_cookies()
            print(f"[auth] вход выполнен как {C.mask(C.USERNAME, 2)}, CSRF {info['csrf_token_masked']}, "
                  f"cookies → {C.LIGHT_STATE_PATH.name}")
        return ok

    def save_cookies(self) -> None:
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                    "secure": c.secure, "expires": c.expires} for c in self.s.cookies]
        C.save_json(C.LIGHT_STATE_PATH, {"base_url": C.BASE_URL, "saved_at": C.now_iso(),
                                         "note": "учебные креды quotes.toscrape.com", "cookies": cookies})

    def load_cookies(self) -> bool:
        if not C.LIGHT_STATE_PATH.exists():
            return False
        data = json.loads(C.LIGHT_STATE_PATH.read_text(encoding="utf-8"))
        if data.get("base_url") != C.BASE_URL:
            return False
        for c in data.get("cookies", []):
            self.s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
        return True


class LightBackend:
    approach = "light"

    def __init__(self, client: LightClient, auth_info: dict):
        self.c, self.auth_info = client, auth_info
        self.parse_ms = 0.0

    def load(self, url: str) -> C.PageResult:
        r0, b0 = self.c.meter.requests, self.c.meter.bytes
        r = self.c.request("GET", url)
        if r.status_code >= 400:
            raise C.PageError(f"http_{r.status_code}", url)
        t0 = time.perf_counter()
        soup = BeautifulSoup(r.text, "html.parser")
        raws = C.extract_quotes_bs(soup)
        nxt = soup.select_one("li.next > a[href]") or next(
            (a for a in soup.select("nav a[href]") if a.get_text(strip=True).startswith("Next")), None)
        auth_ok = any(a.get_text(strip=True) == "Logout" for a in soup.select("a"))
        self.parse_ms += (time.perf_counter() - t0) * 1000
        return C.PageResult(url=r.url, page_no=C.page_no_of(r.url), raws=raws,
                            next_url=urljoin(r.url, nxt["href"]) if nxt else None, auth_ok=auth_ok,
                            status=r.status_code, requests=self.c.meter.requests - r0,
                            bytes=self.c.meter.bytes - b0)

    def follow_next(self, cur: C.PageResult) -> C.PageResult | None:
        return self.load(cur.next_url) if cur.next_url else None

    def reauth(self) -> bool:
        print("[auth] сессия не активна — повторный вход (однократно)")
        self.c.s.cookies.clear()
        return self.c.login(self.auth_info)


def ensure_auth(client: LightClient, info: dict, force_login: bool) -> bool:
    t0 = time.perf_counter()
    ok = False
    if not force_login and client.load_cookies():
        r = client.request("GET", f"{C.BASE_URL}/")
        if client.is_logged_in(r.text):
            info["mode"] = "reused_cookies"
            ok = True
            print(f"[auth] сессия восстановлена из {C.LIGHT_STATE_PATH.name} — повторный вход не нужен")
        else:
            print(f"[auth] {C.LIGHT_STATE_PATH.name} устарел — выполняем вход")
            client.s.cookies.clear()
    if not ok:
        info["mode"] = "fresh_login"
        ok = client.login(info)
    info["auth_s"] = round(time.perf_counter() - t0, 3)
    info["auth_requests"] = client.meter.requests
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force-login", action="store_true")
    ap.add_argument("--max-tags", type=int, default=None)
    ap.add_argument("--min-records", type=int, default=200)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)

    rid = C.run_id()
    log = C.CollectionLog("light", rid)
    meter = HttpMeter()
    auth_info: dict = {}
    metrics: dict = {"run_id": rid, "approach": "light", "base_url": C.BASE_URL, "started_at": C.now_iso()}
    exit_code = 0
    crawler = None
    t0 = time.perf_counter()
    with C.PeakRSS() as mem:
        client = LightClient(meter)
        robots = C.RobotsGuard(C.BASE_URL, client.s)
        print(f"[run {rid}] BASE_URL={C.BASE_URL} | robots.txt: HTTP {robots.status} | "
              f"пауза {robots.pause(C.POLITE_PAUSE)} c")
        try:
            ok = ensure_auth(client, auth_info, args.force_login)
        except C.PageError as e:
            print(f"[auth] сетевой сбой при входе: {e}")
            ok = False
        log.add(stage="auth", section="login", iteration=0, url=f"{C.BASE_URL}/login",
                duration_s=auth_info.get("auth_s"), requests=meter.requests, bytes=meter.bytes,
                status="ok" if ok else "failed", note=auth_info.get("mode", ""))
        if not ok:
            exit_code = 2
        else:
            backend = LightBackend(client, auth_info)
            crawler = C.Crawler(backend=backend, log=log, robots=robots)
            try:
                crawler.run(max_tags=args.max_tags)
            except C.AuthLost as e:
                print(f"[auth] {e}")
                exit_code = 2
            except C.TooManyFailures as e:
                print(f"[stop] превышен лимит сбоев: {e}")
                exit_code = 5
            metrics["parse_ms_total"] = round(backend.parse_ms, 1)
        client.s.close()
    duration = time.perf_counter() - t0
    log.flush()

    if crawler is not None:
        pairs, quotes = C.to_frames(crawler)
        pairs.to_csv(C.OUT_DIR / "data_light.csv", index=False, encoding="utf-8")
        quotes.to_csv(C.OUT_DIR / "quotes_light.csv", index=False, encoding="utf-8")
        q = C.quality_report(pairs)
        warns = C.smoke_check(pairs, name="data_light.csv")
        metrics.update({
            "records": len(pairs), "feed_quotes": len(quotes), "raw_rows": crawler.raw_rows,
            "cycles": crawler.iteration, "failures": crawler.failures, "reauths": crawler.reauths,
            "stop_reasons": crawler.stop_reasons, "quality": q, "smoke_warnings": warns,
            "active_s": round(crawler.active_s, 2), "paused_s": round(crawler.paused_s, 2),
        })
        print(f"\nЗаписей (цитата × тег): {len(pairs)} | уникальных цитат в ленте: {len(quotes)} | "
              f"циклов: {crawler.iteration} | сбоев: {crawler.failures}")
        if exit_code == 0 and len(pairs) < args.min_records:
            exit_code = 3
        if exit_code == 0 and warns and args.strict:
            exit_code = 4
    metrics.update({"duration_s": round(duration, 2), "requests": meter.requests, "bytes": meter.bytes,
                    "mb": round(meter.bytes / 2**20, 3), "peak_rss_mb": mem.peak_mb, "auth": auth_info,
                    "exit_code": exit_code, "finished_at": C.now_iso()})
    if metrics.get("records"):
        metrics["sec_per_record"] = round(duration / metrics["records"], 4)
    C.save_json(C.METRICS / "light.json", metrics)
    print(f"Время прогона: {duration:.1f} c | запросов: {meter.requests} | трафик: {metrics['mb']} МБ | "
          f"пик памяти: {mem.peak_mb} МБ | код возврата: {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
