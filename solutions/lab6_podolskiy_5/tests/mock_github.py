"""Локальная имитация GitHub (API + HTML) на 127.0.0.1 для офлайн-тестов сбора.

Отдаёт синтетические фикстуры из tests/fixtures так же, как настоящий сервис:
* API: /rate_limit, /repos/{o}/{r}, /repos/{o}/{r}/issues с Link-пагинацией,
  параметрами per_page/page/since, заголовками X-RateLimit-* и ответом 403 при
  исчерпании лимита;
* HTML: /robots.txt, /{o}/{r}, /{o}/{r}/releases?page=N.
Для проверки повторов можно попросить один сбой: MOCK_FAIL_ONCE=/repos/psf/requests.

Запуск вручную:
    python tests/mock_github.py --api-port 8765 --web-port 8766
    GITHUB_API_URL=http://127.0.0.1:8765 GITHUB_WEB_URL=http://127.0.0.1:8766 \
    LAB6_DATA_ROOT=/tmp/lab6_test python -m src.collect --html-pause 0.2 --api-pause 0.1
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

FIX = Path(__file__).resolve().parent / "fixtures"


class State:
    def __init__(self, limit: int = 60):
        self.limit = limit
        self.remaining = limit
        self.reset = int(time.time()) + 3600
        self.failed_once: set[str] = set()
        self.lock = threading.Lock()
        self.log: list[str] = []


def _find(folder: Path, owner: str, repo: str, suffix: str = ".json") -> Path | None:
    want = f"{owner}__{repo}{suffix}".lower()
    for p in folder.iterdir():
        if p.name.lower() == want:
            return p
    return None


def make_api_handler(state: State):
    class Api(BaseHTTPRequestHandler):
        def log_message(self, *a):            # тихий режим
            pass

        def _send(self, code: int, body, headers: dict | None = None):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _rate_headers(self) -> dict:
            return {"X-RateLimit-Limit": str(state.limit), "X-RateLimit-Remaining": str(state.remaining),
                    "X-RateLimit-Reset": str(state.reset), "X-RateLimit-Used": str(state.limit - state.remaining),
                    "X-RateLimit-Resource": "core"}

        def do_GET(self):
            parts = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            segs = [s for s in parts.path.split("/") if s]
            state.log.append(self.path)
            if segs == ["rate_limit"]:
                core = {"limit": state.limit, "remaining": state.remaining, "reset": state.reset,
                        "used": state.limit - state.remaining, "resource": "core"}
                return self._send(200, {"resources": {"core": core}, "rate": core})
            fail = os.getenv("MOCK_FAIL_ONCE", "")
            if fail and parts.path == fail and fail not in state.failed_once:
                state.failed_once.add(fail)
                return self._send(502, {"message": "Server Error (mock)"})
            with state.lock:
                if state.remaining <= 0:
                    return self._send(403, {"message": "API rate limit exceeded (mock)"}, self._rate_headers())
                state.remaining -= 1
            if len(segs) >= 3 and segs[0] == "repos":
                owner, repo = segs[1], segs[2]
                if len(segs) == 3:
                    f = _find(FIX / "api" / "repos", owner, repo)
                    if not f:
                        return self._send(404, {"message": "Not Found"}, self._rate_headers())
                    return self._send(200, json.loads(f.read_text()), self._rate_headers())
                if len(segs) == 4 and segs[3] == "issues":
                    f = _find(FIX / "api" / "issues", owner, repo)
                    if not f:
                        return self._send(404, {"message": "Not Found"}, self._rate_headers())
                    items = json.loads(f.read_text())
                    if q.get("since"):
                        items = [i for i in items if i["updated_at"] >= q["since"]]
                    per = min(100, int(q.get("per_page", 30)))
                    page = int(q.get("page", 1))
                    chunk = items[(page - 1) * per: page * per]
                    last = max(1, -(-len(items) // per))
                    base = f"http://{self.headers['Host']}{parts.path}"
                    links = []
                    if page < last:
                        links.append(f'<{base}?{urlencode({**q, "page": page + 1})}>; rel="next"')
                        links.append(f'<{base}?{urlencode({**q, "page": last})}>; rel="last"')
                    hdr = self._rate_headers()
                    if links:
                        hdr["Link"] = ", ".join(links)
                    return self._send(200, chunk, hdr)
            return self._send(404, {"message": "Not Found"}, self._rate_headers())
    return Api


def make_web_handler(state: State):
    class Web(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, text: str, ctype: str = "text/html; charset=utf-8"):
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parts = urlsplit(self.path)
            segs = [s for s in parts.path.split("/") if s]
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            state.log.append("WEB " + self.path)
            if segs == ["robots.txt"]:
                return self._send(200, (FIX / "robots.txt").read_text(encoding="utf-8"), "text/plain")
            if len(segs) == 2:
                f = _find(FIX / "html", segs[0], segs[1], ".html")
                return self._send(200, f.read_text(encoding="utf-8")) if f else self._send(404, "Not Found")
            if len(segs) == 3 and segs[2] == "releases":
                page = int(q.get("page", 1))
                f = _find(FIX / "html", segs[0], segs[1], f"__releases_p{page}.html")
                return self._send(200, f.read_text(encoding="utf-8")) if f else self._send(404, "Not Found")
            return self._send(404, "Not Found")
    return Web


def start(api_port: int = 0, web_port: int = 0, limit: int = 60):
    """Запускает оба сервера в фоновых потоках. Возвращает (api_url, web_url, state, stop)."""
    state = State(limit)
    api = ThreadingHTTPServer(("127.0.0.1", api_port), make_api_handler(state))
    web = ThreadingHTTPServer(("127.0.0.1", web_port), make_web_handler(state))
    for srv in (api, web):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop():
        for srv in (api, web):
            srv.shutdown()
            srv.server_close()
    return (f"http://127.0.0.1:{api.server_address[1]}", f"http://127.0.0.1:{web.server_address[1]}",
            state, stop)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-port", type=int, default=8765)
    ap.add_argument("--web-port", type=int, default=8766)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--seconds", type=int, default=3600, help="сколько секунд работать")
    a = ap.parse_args()
    api_url, web_url, _, stop = start(a.api_port, a.web_port, a.limit)
    print(f"GITHUB_API_URL={api_url}\nGITHUB_WEB_URL={web_url}", flush=True)
    deadline = time.time() + a.seconds
    while time.time() < deadline:        # предохранитель вместо бесконечного цикла
        time.sleep(1)
    stop()
