"""Локальная имитация quotes.toscrape.com для офлайн-проверки (только для тестов).

Повторяет разметку реального сайта (по знанию его шаблонов; подтверждается первым
реальным прогоном):
  * лента  /, /page/N/          — div.quote[itemscope][itemtype=schema.org/CreativeWork],
                                   span.text[itemprop=text], small.author[itemprop=author],
                                   a[href^="/author/"] «(about)», div.tags > meta.keywords + a.tag,
                                   nav > ul.pager > li.next > a «Next →»;
  * теги   /tag/<t>/, /tag/<t>/page/N/ — «Viewing tag: …» + та же карточка;
  * вход   /login                — form[action=/login][method=post] с hidden csrf_token,
                                   input#username, input#password, input[type=submit][value=Login];
                                   метка пароля — <label for="username">Password</label>
                                   (так на реальном сайте; отключается MOCK_LABEL_QUIRK=0);
  * после входа — «Logout» в шапке и «(Goodreads page)» у каждой цитаты;
  * /api/quotes?page=N           — JSON (раздел /scroll реального сайта), goodreads_link без входа;
  * /robots.txt                  — 404, как на реальном сайте.

Данные синтетические (100 цитат, ~50 авторов, ~60 тегов), детерминированные (seed=5).
Сессия — cookie `session`; при успешном входе значение cookie меняется (как у Flask),
CSRF-токен привязан к сессии. Задержка ответа — MOCK_LATENCY_MS (по умолчанию 30 мс).

Запуск:  python tests/mock_site.py --port 8765
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import secrets
import string
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LATENCY = int(os.getenv("MOCK_LATENCY_MS", "30")) / 1000
LABEL_QUIRK = os.getenv("MOCK_LABEL_QUIRK", "1") == "1"
PER_PAGE = 10

# ---------------------------------------------------------------- данные
AUTHORS = [
    ("Albert Einstein", 9810), ("J.K. Rowling", 1077326), ("Jane Austen", 1265),
    ("Marilyn Monroe", 82952), ("André Gide", 7617), ("Thomas A. Edison", 3091287),
    ("Eleanor Roosevelt", 44566), ("Steve Martin", 7103), ("Bob Marley", 57108),
    ("Dr. Seuss", 61105), ("Douglas Adams", 4), ("Elie Wiesel", 1557),
    ("Friedrich Nietzsche", 1938), ("Mark Twain", 1244), ("Allen Saunders", 1255),
    ("Pablo Neruda", 4026), ("Ralph Waldo Emerson", 12080), ("Mother Teresa", 838305),
    ("Garrison Keillor", 3069), ("Jim Henson", 22086), ("Charles M. Schulz", 2),
    ("William Nicholson", 2244), ("Albert Camus", 957894), ("Haruki Murakami", 3354),
    ("Alexandre Dumas fils", 4785), ("Stephenie Meyer", 941441), ("Ernest Hemingway", 1455),
    ("Helen Keller", 7275), ("George Bernard Shaw", 5217), ("Charles Bukowski", 7),
    ("Suzanne Collins", 153394), ("J.R.R. Tolkien", 656983), ("Alfred Tennyson", 3),
    ("Terry Pratchett", 1654), ("J.D. Salinger", 819611), ("George Carlin", 5),
    ("John Lennon", 19968), ("W.C. Fields", 6), ("Ayn Rand", 432), ("Jimi Hendrix", 8),
    ("J.M. Barrie", 9), ("E.E. Cummings", 10), ("Khaled Hosseini", 569),
    ("Harper Lee", 1825), ("Madeleine L'Engle", 106), ("C.S. Lewis", 1069006),
    ("Martin Luther King Jr.", 23924), ("James Baldwin", 10427), ("Stephen Chbosky", 12898),
    ("Sylvia Plath", 4379),
]
TAGS = [
    "love", "inspirational", "life", "humor", "books", "reading", "friendship", "friends",
    "truth", "simile", "change", "deep-thoughts", "thinking", "world", "abilities",
    "choices", "live", "miracle", "miracles", "aliteracy", "classic", "be-yourself",
    "adulthood", "success", "value", "edison", "failure", "paraphrased",
    "misattributed-eleanor-roosevelt", "obvious", "music", "happiness", "dreams",
    "imagination", "knowledge", "learning", "education", "wisdom", "writing", "poetry",
    "philosophy", "faith", "god", "religion", "death", "hope", "fear", "courage",
    "kindness", "children", "fairy-tales", "magic", "fantasy", "romance", "women",
    "attributed-no-source", "sea", "christianity", "heartbreak", "contentment",
]
WORDS = ("world thinking choices love life books truth miracle success failure dream "
         "kindness courage hope humor reading friends music time heart mind").split()


def slug(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in " -" else " " for ch in name)
    return "-".join(keep.split())


def build_data(seed: int = 5) -> list[dict]:
    rnd = random.Random(seed)
    weights = [1 / (i + 1) ** 0.6 for i in range(len(TAGS))]
    quotes = []
    for i in range(1, 101):
        name, gid = AUTHORS[(i * 7) % len(AUTHORS)] if i > 3 else AUTHORS[i - 1]
        n_tags = 0 if i % 23 == 0 else rnd.choice([1, 2, 2, 3, 3, 4, 5])
        tags: list[str] = []
        guard = 0
        while len(tags) < n_tags and guard < 100:   # предохранитель генератора
            guard += 1
            t = rnd.choices(TAGS, weights=weights)[0]
            if t not in tags:
                tags.append(t)
        words = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(6, 14)))
        text = f"Mock quote #{i:03d}: the {words}."
        if i == 4:
            text += " It's \"quoted\" & spaced  twice."     # спецсимволы и двойной пробел
        if i == 7:
            text += "\n        continued on a new line."   # перенос строки внутри текста
        quotes.append({
            "text": f"“{text}”",
            "author": {"name": name, "slug": slug(name),
                       "goodreads_link": f"/author/show/{gid}.{name.replace(' ', '_')}"},
            "tags": tags,
        })
    return quotes


QUOTES = build_data()

# ---------------------------------------------------------------- шаблоны
CSS_MAIN = ".header-box{padding-top:20px}.quote{border:1px solid #333;padding:10px;margin-bottom:30px}\n"
CSS_BOOTSTRAP = "/*! mock bootstrap */\n" + "".join(
    f".c{i}{{margin:{i % 7}px;padding:{i % 5}px;color:#{i % 999:03d}}}\n" for i in range(4500))


def layout(content: str, logged_in: bool) -> str:
    auth = '<a href="/logout">Logout</a>' if logged_in else '<a href="/login">Login</a>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
	<meta charset="UTF-8">
	<title>Quotes to Scrape</title>
    <link rel="stylesheet" href="/static/bootstrap.min.css">
    <link rel="stylesheet" href="/static/main.css">
</head>
<body>
    <div class="container">
        <div class="row header-box">
            <div class="col-md-8">
                <h1>
                    <a href="/" style="text-decoration: none">Quotes to Scrape</a>
                </h1>
            </div>
            <div class="col-md-4">
                <p>
                    {auth}
                </p>
            </div>
        </div>
    {content}
    </div>
    <footer class="footer">
        <div class="container">
            <p class="text-muted">
                Quotes by: <a href="https://www.goodreads.com/quotes">GoodReads.com</a>
            </p>
            <p class="copyright">
                Made with <span class='zyte'>&#10084;</span> by <a class='zyte' href="https://www.zyte.com">Zyte</a>
            </p>
        </div>
    </footer>
</body>
</html>"""


def quote_html(q: dict, logged_in: bool) -> str:
    a = q["author"]
    gr = (f'\n        <a href="http://goodreads.com{html.escape(a["goodreads_link"])}">(Goodreads page)</a>'
          if logged_in else "")
    tags = "".join(
        f'\n            <a class="tag" href="/tag/{t}/page/1/">{t}</a>\n' for t in q["tags"])
    return f"""
    <div class="quote" itemscope itemtype="http://schema.org/CreativeWork">
        <span class="text" itemprop="text">{html.escape(q["text"], quote=False)}</span>
        <span>by <small class="author" itemprop="author">{html.escape(a["name"])}</small>
        <a href="/author/{a["slug"]}">(about)</a>{gr}
        </span>
        <div class="tags">
            Tags:
            <meta class="keywords" itemprop="keywords" content="{",".join(q["tags"])}" /    >
            {tags}
        </div>
    </div>
"""


def top_tags() -> list[tuple[str, int]]:
    cnt: dict[str, int] = {}
    for q in QUOTES:
        for t in q["tags"]:
            cnt[t] = cnt.get(t, 0) + 1
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:10]


def listing(items: list[dict], page: int, prefix: str, logged_in: bool, header: str = "") -> str:
    chunk = items[(page - 1) * PER_PAGE: page * PER_PAGE]
    body = "".join(quote_html(q, logged_in) for q in chunk) if chunk else "\n    No quotes found!\n"
    has_next = page * PER_PAGE < len(items)
    pager = ""
    if page > 1 and chunk:
        pager += f"""
            <li class="previous">
                <a href="{prefix}/page/{page - 1}/"><span aria-hidden="true">&larr;</span> Previous</a>
            </li>"""
    if has_next:
        pager += f"""
            <li class="next">
                <a href="{prefix}/page/{page + 1}/">Next <span aria-hidden="true">&rarr;</span></a>
            </li>"""
    side = "".join(
        f'\n            <span class="tag-item">\n            <a class="tag" style="font-size: {28 - i * 2}px" '
        f'href="/tag/{t}/">{t}</a>\n            </span>' for i, (t, _) in enumerate(top_tags()))
    content = f"""
<div class="row">
    <div class="col-md-8">
{header}
{body}
    <nav>
        <ul class="pager">{pager}
        </ul>
    </nav>
    </div>
    <div class="col-md-4 tags-box">
            <h2>Top Ten tags</h2>{side}
    </div>
</div>
"""
    return layout(content, logged_in)


def login_page(csrf: str, error: str = "") -> str:
    pw_for = "username" if LABEL_QUIRK else "password"
    err = f'\n        <p class="error">{html.escape(error)}</p>' if error else ""
    content = f"""
<div class="row">
    <div class="col-md-12">
    <form action="/login" method="post" accept-charset="utf-8" >
        <input type="hidden" name="csrf_token" value="{csrf}"/>
        <div class="row">
            <div class="form-group col-xs-3">
                <label for="username">Username</label>
                <input type="text" class="form-control" id="username" name="username" />
            </div>
        </div>
        <div class="row">
            <div class="form-group col-xs-3">
                <label for="{pw_for}">Password</label>
                <input type="password" class="form-control" id="password" name="password" />
            </div>
        </div>
        <input type="submit" value="Login" class="btn btn-primary" />{err}
    </form>
    </div>
</div>
"""
    return layout(content, False)


def author_page(name: str, logged_in: bool) -> str:
    content = f"""
<div class="author-details">
    <h3 class="author-title">{html.escape(name)}</h3>
    <p><strong>Born:</strong> <span class="author-born-date">January 1, 1900</span>
    <span class="author-born-location">in Mocktown</span></p>
    <p><strong>Description:</strong></p>
    <div class="author-description">Synthetic author description.</div>
</div>
"""
    return layout(content, logged_in)


# ---------------------------------------------------------------- сервер
SESSIONS: dict[str, dict] = {}
LOCK = threading.Lock()
STATS = {"requests": 0}


def new_csrf() -> str:
    return "".join(secrets.choice(string.ascii_letters) for _ in range(52))


class Handler(BaseHTTPRequestHandler):
    server_version = "MockQuotes/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # тише в консоли
        if os.getenv("MOCK_VERBOSE") == "1":
            super().log_message(fmt, *args)

    # --- helpers
    def _sid(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        c.load(raw)
        return c["session"].value if "session" in c else None

    def _session(self) -> tuple[str | None, dict | None]:
        sid = self._sid()
        with LOCK:
            return sid, SESSIONS.get(sid) if sid else None

    def _send(self, status: int, body: str | bytes, ctype="text/html; charset=utf-8",
              headers: dict | None = None, cookies: list[str] | None = None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, location: str, cookies: list[str] | None = None):
        self._send(302, f'<a href="{location}">{location}</a>', headers={"Location": location},
                   cookies=cookies)

    # --- routing
    def do_GET(self):
        STATS["requests"] += 1
        time.sleep(LATENCY)
        parts = urlsplit(self.path)
        path = parts.path
        segs = [s for s in path.split("/") if s]
        _, sess = self._session()
        logged_in = bool(sess and sess.get("user"))

        if path == "/robots.txt":
            return self._send(404, "<h1>Not Found</h1>")
        if path == "/static/main.css":
            return self._send(200, CSS_MAIN, "text/css")
        if path == "/static/bootstrap.min.css":
            return self._send(200, CSS_BOOTSTRAP, "text/css")
        if path in ("/", ""):
            return self._send(200, listing(QUOTES, 1, "", logged_in))
        if segs and segs[0] == "page" and len(segs) == 2 and segs[1].isdigit():
            return self._send(200, listing(QUOTES, int(segs[1]), "", logged_in))
        if segs and segs[0] == "tag" and len(segs) in (2, 4):
            tag = segs[1]
            page = int(segs[3]) if len(segs) == 4 and segs[2] == "page" and segs[3].isdigit() else 1
            items = [q for q in QUOTES if tag in q["tags"]]
            header = f'    <h3>Viewing tag: <a href="/tag/{tag}/page/1/">{tag}</a></h3>'
            return self._send(200, listing(items, page, f"/tag/{tag}", logged_in, header))
        if segs and segs[0] == "author" and len(segs) == 2:
            names = [a for a, _ in AUTHORS if slug(a) == segs[1]]
            if names:
                return self._send(200, author_page(names[0], logged_in))
            return self._send(404, "<h1>Not Found</h1>")
        if path == "/login":
            sid, sess = self._session()
            cookies = []
            if not sess:
                sid = secrets.token_urlsafe(24)
                sess = {"csrf": new_csrf(), "user": None}
                with LOCK:
                    SESSIONS[sid] = sess
                cookies.append(f"session={sid}; HttpOnly; Path=/")
            return self._send(200, login_page(sess["csrf"]), cookies=cookies)
        if path == "/logout":
            sid, sess = self._session()
            if sess:
                sess["user"] = None
            return self._redirect("/")
        if path == "/api/quotes":
            q = parse_qs(parts.query)
            page = int(q.get("page", ["1"])[0])
            chunk = QUOTES[(page - 1) * PER_PAGE: page * PER_PAGE]
            payload = {"has_next": page * PER_PAGE < len(QUOTES), "page": page,
                       "quotes": chunk, "tag": None, "top_ten_tags": top_tags()}
            return self._send(200, json.dumps(payload), "application/json")
        return self._send(404, "<h1>Not Found</h1>")

    do_HEAD = do_GET

    def do_POST(self):
        STATS["requests"] += 1
        time.sleep(LATENCY)
        if urlsplit(self.path).path != "/login":
            return self._send(405, "Method Not Allowed")
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        get = lambda k: (form.get(k) or [""])[0]
        sid, sess = self._session()
        if not sess or get("csrf_token") != sess["csrf"]:
            # как Flask-WTF: форма не проходит валидацию, вход не выполнен
            fresh = sess["csrf"] if sess else new_csrf()
            return self._send(200, login_page(fresh, "CSRF token missing or incorrect"))
        if not get("username") or not get("password"):
            return self._send(200, login_page(sess["csrf"], "Error while logging in: please, provide your username."))
        new_sid = secrets.token_urlsafe(24)      # значение cookie меняется после входа
        with LOCK:
            SESSIONS.pop(sid, None)
            SESSIONS[new_sid] = {"csrf": new_csrf(), "user": get("username")}
        return self._redirect("/", cookies=[f"session={new_sid}; HttpOnly; Path=/"])


def serve(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    s = serve(args.port)
    pairs = sum(len(q["tags"]) for q in QUOTES)
    print(f"mock quotes.toscrape.com on http://127.0.0.1:{args.port} | quotes={len(QUOTES)} "
          f"pairs(quote×tag)={pairs} tags={len({t for q in QUOTES for t in q['tags']})}", flush=True)
    s.serve_forever()
