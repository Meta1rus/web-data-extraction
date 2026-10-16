"""Локальная ИМИТАЦИЯ github.com/topics и api.github.com для разработки, тестов и демонстрации без сети.

ВНИМАНИЕ: все данные синтетические (владельцы с префиксом fx-), разметка HTML воспроизведена по знанию
вёрстки github.com/topics; цифры, полученные на имитации, НЕ являются результатами исследования.

Что умеет:
  * HTML  (порт 28765): /robots.txt (фикстура), /topics/<topic>?l=python&page=N — 20 карточек на странице,
    сортировка «Most stars», форма «Load more…» на следующую страницу, gzip по Accept-Encoding;
  * API   (порт 28766): /repos/{owner}/{repo} с ETag и 304 на If-None-Match, заголовки X-RateLimit-*
    (304 на авторизованный запрос квоту не расходует — как в документации GitHub), 301 при переименовании,
    /repositories/{id}, /search/repositories с Link-пагинацией, /rate_limit;
  * управление: /__control?version=v2&html_status=503&api_status=503&api_429_every=0&reset=1
    (version=v2 — у 15 репозиториев меняются звёзды, у 5 — pushed_at: демонстрация UPSERT-обновлений);
  * в данные намеренно добавлены проблемные записи (невидимые символы, переименование, дата из будущего).

Запуск:  python demo/mock_github.py [--html-port 28765] [--api-port 28766] [--version v1]
         python demo/mock_github.py --dump-fixtures tests/fixtures   # сохранить фикстуры для pytest
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html as htmlmod
import json
import random
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
TOPICS = ["web-scraping", "fastapi", "machine-learning", "data-engineering", "cli", "telegram"]
PER_PAGE = 20
PAGES = 5
NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)      # фиксированное «сейчас» имитации

TOPIC_PROFILE = {   # параметры синтетических распределений (произвольные, только для имитации)
    "web-scraping": {"stars_mu": 6.6, "size_mu": 8.0, "active": 0.55},
    "fastapi": {"stars_mu": 6.3, "size_mu": 7.2, "active": 0.6},
    "machine-learning": {"stars_mu": 7.6, "size_mu": 10.2, "active": 0.5},
    "data-engineering": {"stars_mu": 6.4, "size_mu": 8.6, "active": 0.58},
    "cli": {"stars_mu": 6.0, "size_mu": 6.4, "active": 0.45},
    "telegram": {"stars_mu": 5.6, "size_mu": 7.0, "active": 0.35},
}
WORDS = ["kit", "flow", "spider", "forge", "lab", "hub", "pipe", "core", "nest", "deck", "pilot", "craft",
         "scope", "byte", "loom", "mint", "wave", "grid", "port", "dash"]
LICENSES = [("mit", "MIT", "MIT License"), ("apache-2.0", "Apache-2.0", "Apache License 2.0"),
            ("bsd-3-clause", "BSD-3-Clause", "BSD 3-Clause \"New\" or \"Revised\" License"),
            ("gpl-3.0", "GPL-3.0", "GNU General Public License v3.0"), ("other", "NOASSERTION", "Other"), None]
EXTRA_TOPICS = ["python", "python3", "api", "automation", "data", "async", "docker", "postgresql", "llm",
                "pandas", "scraping", "bot", "asyncio", "pydantic", "etl", "airflow", "deep-learning",
                "pytorch", "terminal", "rich", "aiogram", "telegram", "selenium", "playwright"]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Dataset:
    def __init__(self, seed: int = 20261015):
        rng = random.Random(seed)
        self.repos: dict[str, dict] = {}            # key (lower full_name) -> repo dict (API-представление)
        self.topic_lists: dict[str, list[str]] = {}
        self.renamed: dict[str, str] = {}           # старое имя (lower) -> новое (lower)
        next_id = 700_000_000
        for ti, topic in enumerate(TOPICS):
            prof = TOPIC_PROFILE[topic]
            keys = []
            for i in range(PER_PAGE * PAGES):
                # ~8 % карточек темы — репозитории, уже встречавшиеся в других темах (пересечение тем)
                if self.repos and rng.random() < 0.08:
                    k = rng.choice(list(self.repos))
                    if k not in keys:
                        self.repos[k]["topics"] = sorted(set(self.repos[k]["topics"]) | {topic})[:20]
                        keys.append(k)
                        continue
                owner = f"fx-{rng.choice(WORDS)}{rng.randint(1, 999)}"
                name = f"{topic.split('-')[0]}-{rng.choice(WORDS)}-{i:03d}"
                created = NOW - timedelta(days=rng.uniform(60, 4800))
                active = rng.random() < prof["active"]
                pushed = NOW - timedelta(days=rng.uniform(0.2, 90) if active else rng.uniform(91, 1800))
                if pushed < created:
                    pushed = created + timedelta(days=rng.uniform(1, 30))
                stars = int(rng.lognormvariate(prof["stars_mu"], 1.1)) + 50
                lic = rng.choice(LICENSES)
                next_id += rng.randint(1, 5000)
                repo = {
                    "id": next_id, "name": name, "full_name": f"{owner}/{name}",
                    "owner": {"login": owner, "id": next_id + 1, "type": rng.choice(["User", "User", "Organization"]),
                              "avatar_url": f"https://avatars.example/u/{next_id}", "site_admin": False},
                    "description": f"Synthetic fixture repository #{i} for topic {topic}",
                    "fork": rng.random() < 0.03, "created_at": iso(created),
                    "updated_at": iso(max(pushed, NOW - timedelta(hours=rng.uniform(1, 400)))),
                    "pushed_at": iso(pushed), "homepage": None,
                    "size": int(rng.lognormvariate(prof["size_mu"], 1.4)),
                    "stargazers_count": stars, "watchers_count": stars,
                    "language": "Python" if rng.random() > 0.04 else rng.choice(["Jupyter Notebook", "HTML"]),
                    "forks_count": int(stars * rng.uniform(0.04, 0.3)),
                    "open_issues_count": int(stars * rng.uniform(0, 0.03)),
                    "archived": rng.random() < 0.05, "disabled": False,
                    "license": None if lic is None else {"key": lic[0], "spdx_id": lic[1], "name": lic[2],
                                                         "url": None, "node_id": "MDc6TGljZW5zZTA="},
                    "topics": sorted({topic, *rng.sample(EXTRA_TOPICS, rng.randint(2, 7))})[:20],
                    "visibility": "public", "default_branch": rng.choice(["main", "master"]),
                    "subscribers_count": int(stars * rng.uniform(0.005, 0.03)) + 1,
                    "network_count": 0, "has_issues": True, "has_wiki": rng.random() < 0.5,
                    "has_discussions": rng.random() < 0.2, "is_template": False,
                    "html_url": f"https://github.com/{owner}/{name}", "url": f"https://api.github.com/repos/{owner}/{name}",
                }
                self.repos[repo["full_name"].lower()] = repo
                keys.append(repo["full_name"].lower())
            self.topic_lists[topic] = keys
        self._inject_problems(rng)

    def _inject_problems(self, rng: random.Random) -> None:
        ws = self.topic_lists["web-scraping"]
        # 1) описание с неразрывными пробелами и невидимым символом — должен исправить шаг нормализации
        r = self.repos[ws[3]]
        r["description"] = "  Fast  scraping​ toolkit\n with   retries  "
        # 2) переименованный репозиторий: страница темы показывает старое имя, API отвечает 301 -> новое
        old_key = ws[7]
        r = self.repos.pop(old_key)
        new_name = r["name"] + "-renamed"
        r["name"], r["full_name"] = new_name, f"{r['owner']['login']}/{new_name}"
        self.repos[r["full_name"].lower()] = r
        self.renamed[old_key] = r["full_name"].lower()
        r["_html_name"] = old_key
        # 3) дата создания из будущего — контракт должен отбраковать запись (ошибка часового пояса у источника)
        self.repos[self.topic_lists["cli"][11]]["created_at"] = iso(NOW + timedelta(days=400))

    def resolve(self, key: str) -> dict | None:
        key = key.lower()
        return self.repos.get(self.renamed.get(key, key))

    def topic_page(self, topic: str, page: int) -> list[tuple[str, dict]]:
        keys = self.topic_lists.get(topic, [])
        pairs = [(k, self.resolve(k)) for k in keys]
        pairs = sorted(pairs, key=lambda kv: -kv[1]["stargazers_count"])
        start = (page - 1) * PER_PAGE
        return pairs[start:start + PER_PAGE]

    def apply_v2(self) -> list[str]:
        """Версия данных v2: изменились звёзды у 15 репозиториев и pushed_at у 5."""
        rng = random.Random(42)
        keys = sorted(self.repos)
        changed = rng.sample(keys, 15)
        for k in changed:
            r = self.repos[k]
            r["stargazers_count"] += rng.randint(1, 30)
            r["watchers_count"] = r["stargazers_count"]
            r["updated_at"] = iso(NOW + timedelta(hours=2))
        for k in rng.sample(keys, 5):
            self.repos[k]["pushed_at"] = iso(NOW + timedelta(hours=1))
            self.repos[k]["updated_at"] = iso(NOW + timedelta(hours=2))
        return changed


# ------------------------------------------------------------------ разметка страницы темы

def _fmt_stars(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def render_card(display_key: str, repo: dict, topic: str) -> str:
    # переименованный репозиторий страница темы показывает под старым именем (кэш поиска GitHub)
    owner, name = (repo.get("_html_name") or repo["full_name"]).split("/")
    stars = repo["stargazers_count"]
    tags = "\n".join(
        f'          <a data-ga-click="Topic, repository list" data-octo-click="topic_click" '
        f'data-octo-dimensions="topic:{t}" href="/topics/{t}" title="Topic: {t}" data-view-component="true" '
        f'class="topic-tag topic-tag-link Link f6 mb-2">\n            {t}\n          </a>' for t in repo["topics"])
    lang = repo.get("language") or ""
    upd = repo["updated_at"]
    upd_text = datetime.fromisoformat(upd.replace("Z", "+00:00")).strftime("%b %d, %Y")
    desc = htmlmod.escape(repo.get("description") or "")
    return f"""
  <article class="border rounded color-shadow-small color-bg-subtle my-4">
    <div class="px-3">
      <div class="d-flex flex-justify-between flex-items-start flex-wrap gap-2 my-3">
        <div class="d-flex flex-1">
          <a href="/{owner}" data-hydro-click="{{&quot;event_type&quot;:&quot;explore.click&quot;}}" data-view-component="true">
            <img class="avatar mr-2 d-none d-md-block avatar-user" src="https://avatars.example/u/{repo['id']}?s=40&amp;v=4" width="20" height="20" alt="@{owner}" />
          </a>
          <h3 class="f3 color-fg-muted text-normal lh-condensed">
            <a data-hydro-click="{{&quot;payload&quot;:{{&quot;click_context&quot;:&quot;REPOSITORY_CARD&quot;}}}}" data-turbo="false" href="/{owner}">
              {owner}
            </a> /
            <a data-hydro-click="{{&quot;payload&quot;:{{&quot;click_target&quot;:&quot;REPOSITORY&quot;}}}}" data-turbo="false" href="/{owner}/{name}" class="text-bold wb-break-word">
              {name}
            </a>
          </h3>
        </div>
        <div class="d-flex flex-items-center">
          <div data-view-component="true" class="BtnGroup d-flex">
            <a href="/login?return_to=%2F{owner}%2F{name}" rel="nofollow" aria-label="You must be signed in to star a repository" data-view-component="true" class="tooltipped tooltipped-sw btn-sm btn">
              <svg aria-hidden="true" height="16" viewBox="0 0 16 16" version="1.1" width="16" data-view-component="true" class="octicon octicon-star v-align-text-bottom d-inline-block mr-2"><path d="M8 .25a.75.75 0 0 1 .673.418Z"></path></svg>
              <span data-view-component="true" class="d-inline">Star</span>
              <span id="repo-stars-counter-star" aria-label="{stars} users starred this repository" data-singular-suffix="user starred this repository" data-plural-suffix="users starred this repository" data-turbo-replace="true" title="{stars:,}" data-view-component="true" class="Counter js-social-count">{_fmt_stars(stars)}</span>
            </a>
          </div>
        </div>
      </div>
    </div>
    <div class="color-bg-default rounded-bottom-2">
      <div class="px-3 pt-3">
        <p class="color-fg-muted mb-0">{desc}</p>
      </div>
      <div class="d-flex flex-wrap border-bottom color-border-muted px-3 pt-2 pb-2">
{tags}
      </div>
      <div class="p-3">
        <ul class="d-flex f6 list-style-none">
          <li class="mr-4">
            <span class="d-inline-block ml-0 mr-3">
              <span class="repo-language-color" style="background-color: #3572A5"></span>
              <span itemprop="programmingLanguage">{lang}</span>
            </span>
          </li>
          <li class="mr-4">
            Updated <relative-time datetime="{upd}" class="no-wrap">{upd_text}</relative-time>
          </li>
        </ul>
      </div>
    </div>
  </article>"""


def render_topic_page(topic: str, page: int, pairs: list, total_cards: int, has_next: bool, lang: str | None) -> str:
    cards = "\n".join(render_card(k, r, topic) for k, r in pairs)
    nxt = ""
    if has_next:
        action = f"/topics/{topic}?" + (f"l={lang}&amp;" if lang else "") + f"page={page + 1}"
        nxt = f"""
  <form class="ajax-pagination-form js-ajax-pagination text-center" action="{action}" accept-charset="UTF-8" method="get">
    {f'<input type="hidden" name="l" value="{lang}" autocomplete="off" />' if lang else ''}
    <button type="submit" data-disable-with="Loading more…" class="ajax-pagination-btn btn btn-outline color-border-default f6 mt-0 width-full">Load more…</button>
  </form>"""
    nonce = secrets.token_hex(16)
    return f"""<!DOCTYPE html>
<!-- FIXTURE: синтетическая страница локальной имитации; разметка воспроизведена по знанию вёрстки github.com/topics -->
<html lang="en" data-color-mode="auto" data-light-theme="light" data-dark-theme="dark">
<head>
  <meta charset="utf-8">
  <title>{topic} · GitHub Topics · GitHub</title>
  <meta name="description" content="GitHub is where people build software.">
  <meta name="csrf-token" content="{nonce}">
  <script type="application/json" id="client-env">{{"locale":"en","featureFlags":["topics_ajax"]}}</script>
</head>
<body class="logged-out env-production page-responsive">
  <div class="application-main" data-commit-hovercards-enabled>
  <main>
    <div class="container-lg p-responsive">
      <div class="d-flex flex-justify-between flex-items-start">
        <h1 class="h1">{topic}</h1>
      </div>
      <p class="f3-light mb-4">Synthetic topic description for {topic}.</p>
    </div>
    <div class="container-lg p-responsive d-flex flex-wrap gutter-md-spacious">
      <div class="col-md-8 col-lg-9">
        <div class="d-flex flex-justify-between flex-items-center mb-3">
          <h2 class="h3 color-fg-muted">
            Here are
            {total_cards:,} public repositories
            matching this topic...
          </h2>
        </div>
{cards}
{nxt}
      </div>
    </div>
  </main>
  </div>
  <footer class="footer pt-8 pb-6 f6 color-fg-muted p-responsive" role="contentinfo">© 2026 GitHub, Inc. (fixture)</footer>
</body>
</html>
"""


def render_react_variant(pairs: list) -> str:
    """Вариант на случай перевода страницы на React: данные только во встроенном JSON (для теста фолбэка)."""
    items = [{"repo": {"owner": {"login": r["owner"]["login"]}, "name": r["name"], "stargazerCount": r["stargazers_count"],
                       "description": r["description"], "language": {"name": r["language"]},
                       "topics": r["topics"], "pushed_at": r["pushed_at"]}} for _, r in pairs]
    payload = json.dumps({"payload": {"topic": "x", "repositories": items}})
    return ("<!DOCTYPE html><html><head><title>topic</title></head><body><div id='root'></div>"
            f"<script type=\"application/json\" data-target=\"react-app.embeddedData\">{payload}</script>"
            + "<p>" + "x" * 6000 + "</p></body></html>")


BLOCK_PAGE = """<!DOCTYPE html><html><head><title>Too many requests</title></head><body>
<h1>Whoa there!</h1><p>You have triggered an abuse detection mechanism. Please wait a few minutes before you try again.</p>
</body></html>"""


# ------------------------------------------------------------------ HTTP-серверы

class MockState:
    def __init__(self, version: str = "v1"):
        self.lock = threading.Lock()
        self.reset(version)

    def reset(self, version: str = "v1") -> None:
        self.data = Dataset()
        self.version = "v1"
        if version == "v2":
            self.data.apply_v2()
            self.version = "v2"
        self.html_status = 200
        self.api_status = 200
        self.api_429_every = 0
        self.api_calls = 0
        self.quota = {"core_auth": 0, "core_anon": 0, "search_auth": 0, "search_anon": 0}


STATE = MockState()


class Handler(BaseHTTPRequestHandler):
    server_version = "MockGitHub/1.0"
    kind = "html"

    def log_message(self, fmt, *args):         # тихий режим
        return

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8",
              headers: dict | None = None) -> None:
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 200
        if gz:
            body = gzip.compress(body)
        self.send_response(status)
        if status != 304:
            self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _control(self, q: dict) -> None:
        with STATE.lock:
            if q.get("reset"):
                STATE.reset(q.get("version", ["v1"])[0])
            elif q.get("version"):
                if q["version"][0] == "v2" and STATE.version != "v2":
                    STATE.data.apply_v2()
                    STATE.version = "v2"
            for k in ("html_status", "api_status", "api_429_every"):
                if q.get(k):
                    setattr(STATE, k, int(q[k][0]))
            info = {"version": STATE.version, "html_status": STATE.html_status, "api_status": STATE.api_status,
                    "api_429_every": STATE.api_429_every, "quota": STATE.quota}
        self._send(200, json.dumps(info).encode(), "application/json")

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/__control":
            return self._control(q)
        if self.kind == "html":
            return self._html(u, q)
        return self._api(u, q)

    # ---- HTML
    def _html(self, u, q):
        if u.path == "/robots.txt":
            return self._send(200, (ROOT / "tests/fixtures/github_robots.txt").read_bytes(), "text/plain; charset=utf-8")
        if STATE.html_status == 429:
            return self._send(429, BLOCK_PAGE.encode(), headers={"Retry-After": "2"})
        if STATE.html_status != 200:
            return self._send(STATE.html_status, b"<html><body>Service Unavailable</body></html>")
        m = re.fullmatch(r"/topics/([a-z0-9-]+)", u.path)
        if not m or m.group(1) not in STATE.data.topic_lists:
            return self._send(404, b"<html><body>Not Found</body></html>")
        topic = m.group(1)
        page = int(q.get("page", ["1"])[0])
        lang = q.get("l", [None])[0]
        pairs = STATE.data.topic_page(topic, page)
        if not pairs:
            return self._send(404, b"<html><body>Not Found</body></html>")
        has_next = page < PAGES
        body = render_topic_page(topic, page, pairs, 1234 + len(topic) * 17, has_next, lang).encode("utf-8")
        etag = 'W/"' + hashlib.md5(body).hexdigest() + '"'     # тело содержит csrf-nonce -> ETag всегда новый
        time.sleep(0.02)
        return self._send(200, body, headers={"ETag": etag, "Cache-Control": "max-age=0, private, must-revalidate"})

    # ---- API
    def _rl_headers(self, resource: str, consume: bool) -> dict:
        auth = bool(self.headers.get("Authorization"))
        key = f"{resource}_{'auth' if auth else 'anon'}"
        limit = {"core": 5000 if auth else 60, "search": 30 if auth else 10}[resource]
        with STATE.lock:
            if consume:
                STATE.quota[key] += 1
            used = STATE.quota[key]
        return {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(max(0, limit - used)),
                "X-RateLimit-Used": str(used), "X-RateLimit-Resource": resource,
                "X-RateLimit-Reset": str(int(time.time()) + 3600), "X-GitHub-Api-Version-Selected": "2022-11-28"}

    def _api(self, u, q):
        with STATE.lock:
            STATE.api_calls += 1
            n = STATE.api_calls
        if STATE.api_status != 200:
            return self._send(STATE.api_status, json.dumps({"message": "Service Unavailable"}).encode(),
                              "application/json")
        if STATE.api_429_every and n % STATE.api_429_every == 0:
            return self._send(429, json.dumps({"message": "You have exceeded a secondary rate limit."}).encode(),
                              "application/json", {"Retry-After": "1", **self._rl_headers("core", False)})
        if u.path == "/rate_limit":
            return self._send(200, json.dumps({"resources": STATE.quota}).encode(), "application/json")
        m = re.fullmatch(r"/repos/([^/]+)/([^/]+)", u.path)
        m2 = re.fullmatch(r"/repositories/(\d+)", u.path)
        if m or m2:
            if m:
                key = f"{m.group(1)}/{m.group(2)}".lower()
                if key in STATE.data.renamed:
                    repo = STATE.data.resolve(key)
                    return self._send(301, json.dumps({"message": "Moved Permanently",
                                                       "url": f"/repositories/{repo['id']}"}).encode(),
                                      "application/json", {"Location": f"/repositories/{repo['id']}",
                                                           **self._rl_headers("core", True)})
                repo = STATE.data.repos.get(key)
            else:
                repo = next((r for r in STATE.data.repos.values() if r["id"] == int(m2.group(1))), None)
            if repo is None:
                return self._send(404, json.dumps({"message": "Not Found"}).encode(), "application/json",
                                  self._rl_headers("core", True))
            body = json.dumps({k: v for k, v in repo.items() if not k.startswith("_")}).encode()
            etag = 'W/"' + hashlib.sha1(body).hexdigest() + '"'
            inm = self.headers.get("If-None-Match")
            auth = bool(self.headers.get("Authorization"))
            if inm and inm == etag:
                # GitHub: 304 на авторизованный запрос не расходует основную квоту
                return self._send(304, b"", headers={"ETag": etag, **self._rl_headers("core", not auth)})
            return self._send(200, body, "application/json; charset=utf-8",
                              {"ETag": etag, "Last-Modified": "Wed, 15 Oct 2026 06:00:00 GMT",
                               **self._rl_headers("core", True)})
        if u.path == "/search/repositories":
            qq = q.get("q", [""])[0]
            tm = re.search(r"topic:([a-z0-9-]+)", qq)
            topic = tm.group(1) if tm else None
            keys = STATE.data.topic_lists.get(topic, [])
            repos = sorted((STATE.data.resolve(k) for k in keys), key=lambda r: -r["stargazers_count"])
            per = int(q.get("per_page", ["30"])[0])
            page = int(q.get("page", ["1"])[0])
            chunk = repos[(page - 1) * per: page * per]
            items = [{k: v for k, v in r.items() if not k.startswith("_") and k not in ("subscribers_count", "network_count")}
                     for r in chunk]
            headers = self._rl_headers("search", True)
            if page * per < len(repos):
                host = self.headers.get("Host")
                headers["Link"] = (f'<http://{host}/search/repositories?q={quote(qq)}&sort=stars&order=desc'
                                   f'&per_page={per}&page={page + 1}>; rel="next"')
            return self._send(200, json.dumps({"total_count": len(repos), "incomplete_results": False,
                                               "items": items}).encode(), "application/json", headers)
        return self._send(404, json.dumps({"message": "Not Found"}).encode(), "application/json")


def make_handler(kind: str):
    return type(f"{kind.title()}Handler", (Handler,), {"kind": kind})


def serve(html_port: int, api_port: int, version: str) -> None:
    STATE.reset(version)
    servers = [ThreadingHTTPServer(("127.0.0.1", html_port), make_handler("html")),
               ThreadingHTTPServer(("127.0.0.1", api_port), make_handler("api"))]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"Имитация GitHub: HTML http://127.0.0.1:{html_port}  API http://127.0.0.1:{api_port}  "
          f"(данные {version}, синтетика)", flush=True)
    try:
        threading.Event().wait()          # ждём Ctrl+C (без цикла while True)
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()


def dump_fixtures(out: Path) -> None:
    """Фикстуры для pytest: страница темы, React-вариант, страница блокировки, ответ API."""
    out.mkdir(parents=True, exist_ok=True)
    data = Dataset()
    pairs = data.topic_page("web-scraping", 1)
    (out / "topic_web-scraping_p1.html").write_text(
        render_topic_page("web-scraping", 1, pairs, 1336, True, "python"), encoding="utf-8")
    (out / "topic_react_variant.html").write_text(render_react_variant(pairs[:5]), encoding="utf-8")
    (out / "blocked_page.html").write_text(BLOCK_PAGE, encoding="utf-8")
    repo = {k: v for k, v in pairs[0][1].items() if not k.startswith("_")}
    (out / "api_repo.json").write_text(json.dumps(repo, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "expected_p1.json").write_text(json.dumps(
        [{"full_name": r.get("_html_name") or r["full_name"], "stars": r["stargazers_count"]} for _, r in pairs],
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("Фикстуры сохранены в", out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html-port", type=int, default=28765)
    ap.add_argument("--api-port", type=int, default=28766)
    ap.add_argument("--version", default="v1", choices=["v1", "v2"])
    ap.add_argument("--dump-fixtures", default=None)
    a = ap.parse_args()
    if a.dump_fixtures:
        dump_fixtures(Path(a.dump_fixtures))
        return 0
    serve(a.html_port, a.api_port, a.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
