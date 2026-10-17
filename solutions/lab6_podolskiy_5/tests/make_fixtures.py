"""Генератор СИНТЕТИЧЕСКИХ фикстур GitHub для разработки и тестов без сети.

Почему синтетика: в среде разработки api.github.com и github.com были закрыты
сетевой политикой (HTTP 403 от прокси), поэтому реальные ответы сохранить не
удалось. Фикстуры повторяют СТРУКТУРУ реальных ответов:
* JSON — поля и форматы /repos/{o}/{r} и /repos/{o}/{r}/issues (REST API 2022-11-28);
* HTML — разметку страницы репозитория и страницы релизов github.com,
  какой она известна автору на 2025–2026 гг.
Значения (звёзды, номера, заголовки, даты) — ВЫДУМАННЫЕ, хотя и правдоподобные.
Использовать их как реальные данные нельзя.

Запуск: python tests/make_fixtures.py   (детерминированно, seed=5)
"""
from __future__ import annotations

import hashlib
import html
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
rng = random.Random(5)

LABELS = [("bug", "d73a4a"), ("documentation", "0075ca"), ("enhancement", "a2eeef"),
          ("help wanted", "008672"), ("good first issue", "7057ff"), ("question", "d876e3"),
          ("dependencies", "0366d6"), ("needs triage", "ededed"), ("performance", "5319e7"),
          ("CI", "000000"), ("regression", "b60205"), ("python", "2b67c6")]

# slug, id, created, lang, license(spdx), stars, forks, watchers, open_issues, size, homepage,
# description, topics, releases_count, used_by(text,title), contributors(text,title), languages, tag_fmt,
# last issue number
REPOS = [
    ("scrapy/scrapy", 529502, "2010-02-22T02:01:14Z", "Python", "BSD-3-Clause", 55120, 10880, 1752, 512, 26890,
     "https://scrapy.org", "Scrapy, a fast high-level web crawling & scraping framework for Python.",
     ["python", "crawler", "framework", "scraping", "crawling", "web-scraping", "hacktoberfest"], 58,
     ("54.1k", "54,118"), ("592", "592"), [("Python", 99.8), ("HTML", 0.1), ("Other", 0.1)], "{v}", 7110),
    ("psf/requests", 1362490, "2011-02-13T18:38:17Z", "Python", "Apache-2.0", 53310, 9590, 1318, 241, 13640,
     "https://requests.readthedocs.io", "A simple, yet elegant, HTTP library.",
     ["python", "http", "forhumans", "requests", "python-requests", "client", "cookies"], 32,
     ("3.4m", "3,412,905"), ("769", "769"), [("Python", 99.9), ("Makefile", 0.1)], "v{v}", 7040),
    ("microsoft/playwright-python", 276385740, "2020-07-01T09:11:12Z", "Python", "Apache-2.0", 13780, 1050,
     143, 61, 5200, "https://playwright.dev/python/",
     "Python version of the Playwright testing and automation library.",
     ["python", "testing", "automation", "playwright", "chromium", "firefox", "webkit"], 98,
     ("61.2k", "61,240"), ("71", "71"), [("Python", 98.4), ("JavaScript", 1.2), ("Shell", 0.4)], "v{v}", 2980),
    ("SeleniumHQ/selenium", 7613257, "2013-01-14T18:05:48Z", "Java", "Apache-2.0", 32610, 8620, 1291, 168,
     391220, "https://selenium.dev", "A browser automation framework and ecosystem.",
     ["javascript", "ruby", "python", "java", "rust", "firefox", "dotnet", "selenium", "webdriver"], 121,
     ("312k", "312,447"), ("794", "794"),
     [("Java", 34.9), ("C#", 15.4), ("Python", 12.8), ("Ruby", 11.0), ("JavaScript", 10.6), ("Starlark", 6.1),
      ("Other", 9.2)], "selenium-{v}", 16410),
    ("n8n-io/n8n", 193215554, "2019-06-22T09:24:21Z", "TypeScript", "NOASSERTION", 149800, 47100, 901, 1210,
     512000, "https://n8n.io",
     "Fair-code workflow automation platform with native AI capabilities. Combine visual building with "
     "custom code, self-host or cloud, 400+ integrations.",
     ["automation", "workflow", "low-code", "no-code", "typescript", "ai", "integrations", "self-hosted"], 812,
     None, ("528", "528"), [("TypeScript", 90.9), ("Vue", 7.5), ("SCSS", 0.6), ("JavaScript", 0.5),
                            ("Other", 0.5)], "n8n@{v}", 20410),
    ("pandas-dev/pandas", 858127, "2010-08-24T01:37:33Z", "Python", "BSD-3-Clause", 46210, 18810, 1118, 3605,
     358900, "https://pandas.pydata.org",
     "Flexible and powerful data analysis / manipulation library for Python, providing labeled data "
     "structures similar to R data.frame objects, statistical functions, and much more",
     ["python", "data-science", "pandas", "data-analysis", "flexible", "alignment", "hacktoberfest"], 142,
     ("1.6m", "1,603,778"), ("3.4k", "3,402"), [("Python", 90.6), ("Cython", 5.2), ("C", 1.9),
                                               ("HTML", 1.2), ("Other", 1.1)], "v{v}", 62480),
    ("pydantic/pydantic", 87393640, "2017-05-03T21:23:58Z", "Python", "MIT", 25640, 2240, 119, 498, 36120,
     "https://docs.pydantic.dev", "Data validation using Python type hints",
     ["python", "json", "json-schema", "validation", "parsing", "hints", "pydantic"], 171,
     ("612k", "612,330"), ("608", "608"), [("Python", 99.9), ("Other", 0.1)], "v{v}", 12350),
    ("apache/arrow", 51905353, "2016-02-17T08:00:23Z", "C++", "Apache-2.0", 16020, 3890, 341, 4602, 251800,
     "https://arrow.apache.org/",
     "Apache Arrow is the universal columnar format and multi-language toolbox for fast data interchange "
     "and in-memory analytics",
     ["arrow", "parquet", "big-data", "in-memory", "dataframe"], 41,
     ("331k", "331,076"), ("1,342", "1,342"), [("C++", 46.1), ("Python", 13.3), ("Ruby", 9.7), ("Cython", 7.4),
                                               ("C", 6.1), ("CMake", 3.3), ("Other", 14.1)],
     "apache-arrow-{v}", 47920),
    ("duckdb/duckdb", 138754790, "2018-06-26T15:04:45Z", "C++", "MIT", 31380, 2530, 229, 412, 314500,
     "https://www.duckdb.org", "DuckDB is an analytical in-process SQL database management system",
     ["sql", "database", "olap", "embedded-database", "analytics"], 63,
     ("20.4k", "20,391"), ("482", "482"), [("C++", 81.4), ("Python", 7.9), ("C", 6.2), ("Other", 4.5)],
     "v{v}", 19310),
    ("encode/httpx", 186410573, "2019-04-04T10:16:16Z", "Python", "BSD-3-Clause", 14560, 970, 108, 92, 3800,
     "https://www.python-httpx.org/", "A next generation HTTP client for Python. 🦋",
     ["python", "http", "asyncio", "trio", "http2"], 74,
     ("912k", "912,045"), ("231", "231"), [("Python", 99.6), ("Shell", 0.4)], "{v}", 3640),
    ("aio-libs/aiohttp", 13258039, "2013-10-01T15:57:41Z", "Python", "NOASSERTION", 15790, 2110, 218, 248,
     34900, "https://docs.aiohttp.org", "Asynchronous HTTP client/server framework for asyncio and Python",
     ["python", "http", "asyncio", "aiohttp", "http-client", "http-server"], 249,
     ("644k", "644,210"), ("754", "754"), [("Python", 93.7), ("Cython", 3.8), ("C", 1.6), ("Other", 0.9)],
     "v{v}", 11420),
    ("scrapy/parsel", 18447412, "2014-04-03T21:10:05Z", "Python", "BSD-3-Clause", 1251, 156, 41, 34, 980,
     "https://parsel.readthedocs.io",
     "Parsel lets you extract data from XML/HTML documents using XPath or CSS selectors",
     ["python", "xpath", "css-selectors", "parsing", "html", "xml"], 31,
     ("131k", "131,412"), ("52", "52"), [("Python", 100.0)], "v{v}", 330),
]

COMPONENTS = ["downloader", "spider middleware", "selector", "parser", "cache", "scheduler", "CLI", "logging",
              "type hints", "test suite", "docs build", "CI pipeline", "async client", "connection pool",
              "timezone handling", "date parsing", "CSV export", "Parquet writer", "proxy support", "retries"]
VERBS = ["fails", "hangs", "raises TypeError", "leaks memory", "returns wrong result", "is slow",
         "ignores timeout", "crashes"]
CONDS = ["on Python 3.13", "with HTTP/2", "on Windows", "when the response is empty", "with non-UTF-8 pages",
         "under high concurrency", "after upgrade", "with naive datetimes", "on macOS arm64",
         "when the URL has query params"]
FEATS = ["async iteration", "custom retry policy", "per-request timeouts", "streaming uploads",
         "typed settings", "nullable integers", "UTC-aware timestamps", "JSON Lines export",
         "rate-limit headers", "configurable backoff"]
DEPS = ["pytest", "mypy", "ruff", "sphinx", "actions/checkout", "actions/setup-python", "black", "coverage",
        "pre-commit", "setuptools"]


def iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def fake_login(seed: str) -> str:
    return "dev-" + hashlib.sha1(seed.encode()).hexdigest()[:8]


def gen_title(kind: str, bot: str | None) -> str:
    if bot == "dependabot[bot]":
        d = rng.choice(DEPS)
        a, b = rng.randint(1, 8), rng.randint(0, 12)
        return f"Bump {d} from {a}.{b}.0 to {a}.{b + 1}.0"
    if bot == "pre-commit-ci[bot]":
        return "[pre-commit.ci] pre-commit autoupdate"
    r = rng.random()
    if kind == "pull_request":
        if r < 0.35:
            return f"Fix {rng.choice(COMPONENTS)} that {rng.choice(VERBS)} {rng.choice(CONDS)}"
        if r < 0.7:
            return f"Add {rng.choice(FEATS)} to {rng.choice(COMPONENTS)}"
        if r < 0.85:
            return f"DOC: clarify {rng.choice(COMPONENTS)} section"
        return f"Refactor {rng.choice(COMPONENTS)}"
    if r < 0.5:
        return f"BUG: {rng.choice(COMPONENTS)} {rng.choice(VERBS)} {rng.choice(CONDS)}"
    if r < 0.8:
        return f"ENH: support {rng.choice(FEATS)}"
    if r < 0.9:
        return f"Question: how to configure {rng.choice(COMPONENTS)}?"
    return f"DOC: typo in {rng.choice(COMPONENTS)} docs"


def gen_issues(slug: str, created_repo: datetime, last_number: int) -> list[dict]:
    owner, name = slug.split("/")
    n = rng.randint(240, 330)
    items = []
    used_numbers: set[int] = set()
    for i in range(n):
        u = rng.random()
        if u < 0.7:
            age_days = rng.uniform(0, 180)
        elif u < 0.9:
            age_days = rng.uniform(180, 720)
        else:
            age_days = rng.uniform(720, (NOW - created_repo).days - 1)
        created = NOW - timedelta(days=age_days, seconds=rng.randint(0, 86399))
        # номер растёт со временем создания
        frac = 1 - age_days / max(1, (NOW - created_repo).days)
        number = max(1, int(last_number * frac) - rng.randint(0, 25))
        while number in used_numbers:
            number += 1
        used_numbers.add(number)
        is_pr = rng.random() < 0.45
        bot = None
        if is_pr and rng.random() < 0.15:
            bot = rng.choice(["dependabot[bot]", "dependabot[bot]", "pre-commit-ci[bot]"])
        kind = "pull_request" if is_pr else "issue"
        closed_p = 0.72 if is_pr else 0.55
        closed = rng.random() < closed_p
        closed_at = merged_at = None
        state_reason = None
        if closed:
            hours = rng.lognormvariate(3.6 if is_pr else 5.0, 1.6)
            closed_at = created + timedelta(hours=hours)
            if closed_at >= NOW:
                closed_at = NOW - timedelta(minutes=rng.randint(5, 600))
            if closed_at < created:
                closed_at = created + timedelta(minutes=7)
            if is_pr:
                if rng.random() < 0.75:
                    merged_at = closed_at
            else:
                state_reason = "completed" if rng.random() < 0.75 else "not_planned"
        elif not is_pr and rng.random() < 0.03:
            state_reason = "reopened"
        last_touch = closed_at or created
        updated = last_touch + timedelta(hours=rng.uniform(0, 400))
        if updated > NOW:
            updated = NOW - timedelta(minutes=rng.randint(1, 300))
        if updated < last_touch:
            updated = last_touch
        login = bot or fake_login(f"{slug}{i}{rng.random()}")
        assoc = "NONE" if bot else rng.choices(
            ["MEMBER", "CONTRIBUTOR", "NONE", "COLLABORATOR", "FIRST_TIME_CONTRIBUTOR", "OWNER"],
            [25, 35, 30, 5, 4, 1])[0]
        labels = [] if bot else rng.sample(LABELS, k=rng.choices([0, 1, 2], [40, 45, 15])[0])
        if bot == "dependabot[bot]":
            labels = [("dependencies", "0366d6")]
        item = {
            "url": f"https://api.github.com/repos/{owner}/{name}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{owner}/{name}",
            "html_url": f"https://github.com/{owner}/{name}/{'pull' if is_pr else 'issues'}/{number}",
            "id": 1_000_000_000 + rng.randint(0, 2_999_999_999),
            "node_id": ("PR_kwDO" if is_pr else "I_kwDO") + hashlib.md5(f"{slug}{number}".encode()).hexdigest()[:12],
            "number": number,
            "title": gen_title(kind, bot),
            "user": {"login": login, "id": rng.randint(1000, 99_999_999),
                     "avatar_url": f"https://avatars.githubusercontent.com/u/{rng.randint(1000, 9999999)}?v=4",
                     "type": "Bot" if bot else "User", "site_admin": False},
            "labels": [{"id": rng.randint(1, 10**9), "name": ln, "color": lc, "default": ln in
                        ("bug", "documentation", "enhancement", "help wanted", "good first issue", "question")}
                       for ln, lc in labels],
            "state": "closed" if closed else "open",
            "locked": rng.random() < 0.02,
            "assignee": None, "assignees": [], "milestone": None,
            "comments": min(int(rng.expovariate(1 / 3.5)), 120),
            "created_at": iso(created), "updated_at": iso(updated), "closed_at": iso(closed_at),
            "author_association": assoc,
            "active_lock_reason": None,
            "body": "Synthetic body text (fixture).",
            "reactions": {"total_count": int(rng.expovariate(1 / 1.5)), "+1": 0, "-1": 0},
            "timeline_url": f"https://api.github.com/repos/{owner}/{name}/issues/{number}/timeline",
            "performed_via_github_app": None,
            "state_reason": state_reason,
        }
        if is_pr:
            item["draft"] = (not closed) and rng.random() < 0.15
            item["pull_request"] = {
                "url": f"https://api.github.com/repos/{owner}/{name}/pulls/{number}",
                "html_url": item["html_url"],
                "diff_url": item["html_url"] + ".diff", "patch_url": item["html_url"] + ".patch",
                "merged_at": iso(merged_at)}
        items.append(item)
    items.sort(key=lambda d: d["updated_at"], reverse=True)    # как при sort=updated&direction=desc
    return items


def version_list(slug: str, count: int) -> list[str]:
    major, minor, patch = rng.randint(1, 4), rng.randint(5, 40), rng.randint(0, 6)
    out = []
    for _ in range(count):
        out.append(f"{major}.{minor}.{patch}")
        if patch > 0:
            patch -= 1
        else:
            minor = max(0, minor - 1)
            patch = rng.randint(0, 5)
    return out


def gen_releases(slug: str, tag_fmt: str, total: int) -> list[dict]:
    vers = version_list(slug, min(total, 20))
    t = NOW - timedelta(days=rng.uniform(1, 25))
    rels = []
    for i, v in enumerate(vers):
        tag = tag_fmt.format(v=v)
        sha = "".join(rng.choice("0123456789abcdef") for _ in range(7))
        if i == 3:
            sha = "0" + sha[1:]                       # короткий SHA с ведущим нулём
        pre = slug == "n8n-io/n8n" and i % 3 == 0 and i > 0
        rels.append({"tag": tag, "title": (f"{slug.split('/')[1]} {v}" if i % 2 else tag),
                     "published": iso(t), "sha": sha, "prerelease": pre, "latest": i == 0})
        t -= timedelta(days=rng.uniform(3, 45))
    return rels


# ------------------------------------------------------------------ HTML-шаблоны
HEAD = """<!DOCTYPE html>
<html lang="en" data-color-mode="auto" data-light-theme="light" data-dark-theme="dark">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <meta name="description" content="{meta_desc}">
  <meta property="og:title" content="{og_title}">
  <meta property="og:url" content="https://github.com/{slug}">
  <meta name="octolytics-dimension-repository_id" content="{rid}">
  <meta name="octolytics-dimension-repository_nwo" content="{slug}">
  <meta name="octolytics-dimension-repository_public" content="true">
  <meta name="octolytics-dimension-repository_is_fork" content="false">
  <link crossorigin="anonymous" media="all" rel="stylesheet" href="https://github.githubassets.com/assets/primer-fixture.css">
  <script type="application/json" id="client-env">{{"locale":"en","featureFlags":["fixture"]}}</script>
</head>
<body class="logged-out env-production page-responsive">
<div class="logged-out env-production page-responsive" style="word-wrap: break-word;">
  <header class="HeaderMktg header-logged-out js-details-container js-header Details f4 py-3" role="banner">
    <a href="/login?return_to=https%3A%2F%2Fgithub.com%2F{slug_q}" class="HeaderMenu-link HeaderMenu-link--sign-in">Sign in</a>
  </header>
  <main id="js-repo-pjax-container">
"""

REPO_HEADER = """    <div id="repository-container-header" class="pt-3 hide-full-screen" style="background-color: var(--page-header-bgColor, var(--color-page-header-bg));" data-turbo-replace>
      <div class="d-flex flex-nowrap flex-justify-end mb-3 px-3 px-lg-5" style="gap: 1rem;">
        <div class="flex-auto min-width-0 width-fit">
          <div class="d-flex flex-items-center flex-wrap f3">
            <span class="author flex-self-stretch" itemprop="author"><a class="url fn" rel="author" href="/{owner}">{owner}</a></span>
            <span class="mx-1 flex-self-stretch color-fg-muted">/</span>
            <strong itemprop="name" class="mr-2 flex-self-stretch"><a data-pjax="#repo-content-pjax-container" href="/{slug}">{name}</a></strong>
            <span class="Label Label--secondary v-align-middle mr-1">Public</span>
          </div>
        </div>
        <div id="repository-details-container" class="flex-shrink-0" data-turbo-replace style="max-width: 70%;">
          <ul class="pagehead-actions flex-shrink-0 d-none d-md-inline" style="padding: 2px 0;">
            <li><a href="/login?return_to=%2F{slug_q}" rel="nofollow" class="btn-sm btn">
              <svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-repo-forked mr-2"><path d="M5 5.372v.878c0"></path></svg>Fork
              <span id="repo-network-counter" data-pjax-replace="true" data-turbo-replace="true" title="{forks_exact}" data-view-component="true" class="Counter">{forks_short}</span>
            </a></li>
            <li><div data-view-component="true" class="BtnGroup d-flex">
              <a href="/login?return_to=%2F{slug_q}" rel="nofollow" class="tooltipped tooltipped-sw btn-sm btn" aria-label="You must be signed in to star a repository">
              <svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-star v-align-text-bottom d-inline-block mr-2"><path d="M8 .25a.75.75 0"></path></svg>
              <span data-view-component="true" class="d-inline">Star</span>
              <span id="repo-stars-counter-star" aria-label="{stars_plain} users starred this repository" data-singular-suffix="user starred this repository" data-plural-suffix="users starred this repository" data-turbo-replace="true" title="{stars_exact}" data-view-component="true" class="Counter js-social-count">{stars_short}</span>
              </a></div></li>
          </ul>
        </div>
      </div>
    </div>
"""

REPO_HEADER_NO_COUNTERS = """    <div id="repository-container-header" class="pt-3 hide-full-screen" data-turbo-replace>
      <div class="d-flex flex-wrap flex-items-center f3"><a class="url fn" rel="author" href="/{owner}">{owner}</a> / <strong itemprop="name"><a href="/{slug}">{name}</a></strong></div>
    </div>
"""


def short(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m".replace(".0m", "m")
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def repo_page(r: tuple, rels: list[dict], variant: str) -> str:
    (slug, rid, _created, _lang, spdx, stars, forks, watchers, _oi, _size, homepage, desc, topics, rel_count,
     used_by, contrib, langs, _fmt, _last) = r
    owner, name = slug.split("/")
    title = f"GitHub - {slug}: {desc}"
    meta_desc = f"{desc} - {slug}"
    parts = [HEAD.format(title=html.escape(title), meta_desc=html.escape(meta_desc), og_title=html.escape(title),
                         slug=slug, rid=rid, slug_q=quote(slug, safe=""))]
    if variant == "no_header_counters":
        parts.append(REPO_HEADER_NO_COUNTERS.format(owner=owner, name=name, slug=slug))
    else:
        parts.append(REPO_HEADER.format(owner=owner, name=name, slug=slug, slug_q=quote(slug, safe=""),
                                        forks_exact=f"{forks:,}", forks_short=short(forks),
                                        stars_exact=f"{stars:,}", stars_plain=stars, stars_short=short(stars)))
    # React-часть со списком файлов: на реальной странице это большой JSON — здесь заглушка
    parts.append("""    <div id="repo-content-pjax-container" class="repository-content">
      <react-partial partial-name="repos-overview" data-ssr="true">
        <script type="application/json" data-target="react-partial.embeddedData">{"props":{"initialPayload":{"refInfo":{"name":"main"},"overview":{"commitCount":"fixture"}}}}</script>
      </react-partial>
      <div class="Layout-sidebar">
      <div class="BorderGrid about-margin" data-pjax>
        <div class="BorderGrid-row">
          <div class="BorderGrid-cell">
            <div class="hide-sm hide-md">
              <h2 class="mb-3 h4">About</h2>
""")
    if variant != "no_about":
        parts.append(f'              <p class="f4 my-3">\n                {html.escape(desc)}\n              </p>\n')
    else:
        parts.append('              <div class="f4 my-3 color-fg-muted text-italic">No description, website, or topics provided.</div>\n')
    parts.append(f"""              <div class="my-3 d-flex flex-items-center">
                <svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-link flex-shrink-0 mr-2"><path d="m7.775 3.275"></path></svg>
                <span class="flex-auto min-width-0 css-truncate css-truncate-target width-fit">
                  <a title="{homepage}" role="link" target="_blank" class="text-bold" rel="noopener noreferrer" href="{homepage}">{homepage.split('//', 1)[1].rstrip('/')}</a>
                </span>
              </div>
              <h3 class="sr-only">Topics</h3>
              <div class="my-3">
                <div class="f6">
""")
    for t in topics:
        parts.append(f'      <a href="/topics/{t}" title="Topic: {t}" data-view-component="true" class="topic-tag topic-tag-link">\n  {t}\n</a>\n')
    lic_text = {"NOASSERTION": "View license"}.get(spdx, f"{spdx} license")
    parts.append(f"""                </div>
              </div>
              <h3 class="sr-only">Resources</h3>
              <div class="mt-2">
                <a class="Link--muted" href="#readme-ov-file"><svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-book mr-2"><path d="M0 1.75A.75.75 0"></path></svg>Readme</a>
              </div>
              <h3 class="sr-only">License</h3>
              <div class="mt-2">
                <a href="#{spdx}-1-ov-file" class="Link--muted"><svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-law mr-2"><path d="M8.75.75V2h.985"></path></svg>
                 {lic_text}
                </a>
              </div>
              <h3 class="sr-only">Stars</h3>
              <div class="mt-2">
                <a href="/{slug}/stargazers" data-view-component="true" class="Link Link--muted">
                  <svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-star mr-2"><path d="M8 .25a.75.75 0"></path></svg>
                  <strong>{short(stars)}</strong>
                  stars
                </a>
              </div>
              <h3 class="sr-only">Watchers</h3>
              <div class="mt-2">
                <a href="/{slug}/watchers" data-view-component="true" class="Link Link--muted">
                  <svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-eye mr-2"><path d="M8 2c1.981 0"></path></svg>
                  <strong>{short(watchers)}</strong>
                  watching
                </a>
              </div>
              <h3 class="sr-only">Forks</h3>
              <div class="mt-2">
                <a href="/{slug}/forks" data-view-component="true" class="Link Link--muted">
                  <svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-repo-forked mr-2"><path d="M5 5.372v.878c0"></path></svg>
                  <strong>{short(forks)}</strong>
                  forks
                </a>
              </div>
            </div>
          </div>
        </div>
""")
    latest = rels[0]
    parts.append(f"""        <div class="BorderGrid-row">
          <div class="BorderGrid-cell">
            <h2 class="h4 mb-3" data-pjax="#repo-content-pjax-container" data-turbo-frame="repo-content-turbo-frame">
              <a href="/{slug}/releases" data-view-component="true" class="Link--primary no-underline Link">
                Releases
                <span title="{rel_count}" data-view-component="true" class="Counter">{rel_count}</span>
              </a></h2>
            <a class="Link--primary d-flex no-underline" data-pjax="#repo-content-pjax-container" data-turbo-frame="repo-content-turbo-frame" href="/{slug}/releases/tag/{quote(latest['tag'], safe='')}">
              <svg aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-tag flex-shrink-0 mt-1 color-fg-success"><path d="M1 7.775V2.75C1"></path></svg>
              <div class="ml-2 min-width-0">
                <div class="d-flex">
                  <span class="css-truncate css-truncate-target text-bold mr-2" style="max-width: none;">{html.escape(latest['title'])}</span>
                  <span title="Label: Latest" data-view-component="true" class="Label Label--success flex-shrink-0">
                    Latest
                  </span>
                </div>
                <div class="text-small color-fg-muted"><relative-time datetime="{latest['published']}" class="no-wrap">{latest['published'][:10]}</relative-time></div>
              </div>
            </a>
            <div data-view-component="true" class="mt-3">
              <a text="small" href="/{slug}/releases" data-view-component="true" class="Link">+ {rel_count - 1} releases</a>
            </div>
          </div>
        </div>
""")
    if used_by is not None:
        parts.append(f"""        <div class="BorderGrid-row">
          <div class="BorderGrid-cell">
            <h2 class="h4 mb-3">
              <a href="/{slug}/network/dependents?package_id=UGFja2FnZS0xMjM0NTY%3D" data-view-component="true" class="Link--primary no-underline Link">
                Used by <span title="{used_by[1]}" data-view-component="true" class="Counter">{used_by[0]}</span>
              </a>
            </h2>
            <a class="d-flex flex-items-center no-underline" href="/{slug}/network/dependents?package_id=UGFja2FnZS0xMjM0NTY%3D">
              <span class="px-2 text-bold text-small no-wrap">+ {used_by[1]}</span>
            </a>
          </div>
        </div>
""")
    parts.append(f"""        <div class="BorderGrid-row">
          <div class="BorderGrid-cell">
            <h2 class="h4 mb-3">
              <a href="/{slug}/graphs/contributors" data-view-component="true" class="Link--primary no-underline Link d-flex flex-items-center">
                Contributors
                <span title="{contrib[1]}" data-view-component="true" class="Counter ml-1">{contrib[0]}</span>
              </a>
            </h2>
            <ul class="list-style-none d-flex flex-wrap mb-n2">
              <li class="mb-2 mr-2"><a href="https://github.com/fixture-user" class="" data-hovercard-type="user"><img src="https://avatars.githubusercontent.com/u/1?s=64&amp;v=4" alt="@fixture-user" size="32" height="32" width="32" class="avatar circle"></a></li>
            </ul>
          </div>
        </div>
        <div class="BorderGrid-row">
          <div class="BorderGrid-cell">
            <h2 class="h4 mb-3">Languages</h2>
            <div class="mb-2">
              <span data-view-component="true" class="Progress">
""")
    for lang, pct in langs:
        parts.append(f'                <span style="background-color:#3572A5 !important;;width: {pct}%;" itemprop="keywords" aria-label="{lang} {pct}" data-view-component="true" class="Progress-item color-bg-success-emphasis"></span>\n')
    parts.append('              </span>\n            </div>\n            <ul class="list-style-none">\n')
    if variant == "languages_bar_only":
        parts.append("            </ul>\n")
    else:
        for lang, pct in langs:
            parts.append(f"""                <li class="d-inline">
                  <a class="d-inline-flex flex-items-center flex-nowrap Link--secondary no-underline text-small mr-3" href="/{slug}/search?l={quote(lang.lower())}" data-ga-click="Repository, language stats search click, location:repo overview">
                    <svg style="color:#3572A5;" aria-hidden="true" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-dot-fill mr-2"><path d="M8 4a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"></path></svg>
                    <span class="color-fg-default text-bold mr-1">{html.escape(lang)}</span>
                    <span>{pct}%</span>
                  </a>
                </li>
""")
        parts.append("            </ul>\n")
    parts.append("""          </div>
        </div>
      </div>
      </div>
    </div>
  </main>
  <footer class="footer pt-8 pb-6 f6 color-fg-muted p-responsive" role="contentinfo">© 2026 GitHub, Inc. (fixture)</footer>
</div>
</body>
</html>
""")
    return "".join(parts)


def releases_page(r: tuple, rels: list[dict], page: int, has_next: bool, variant: str) -> str:
    slug, rid, desc = r[0], r[1], r[11]
    owner, name = slug.split("/")
    parts = [HEAD.format(title=f"Releases · {slug} · GitHub", meta_desc=html.escape(f"{desc} - {slug}"),
                         og_title=f"Releases · {slug}", slug=slug, rid=rid, slug_q=quote(slug, safe=""))]
    parts.append('    <div id="repo-content-pjax-container" class="repository-content">\n'
                 '      <h1 class="sr-only">Releases: ' + slug + '</h1>\n'
                 '      <div data-hpc id="release_page_title"><div class="d-flex flex-column flex-md-row">\n'
                 '        <nav class="subnav-links"><a href="/' + slug + '/releases" class="js-selected-navigation-item selected subnav-item">Releases</a>\n'
                 '        <a href="/' + slug + '/tags" class="subnav-item">Tags</a></nav></div></div>\n')
    for rel in rels:
        tag_q = quote(rel["tag"], safe="")
        labels = []
        if rel["latest"]:
            labels.append('<span data-view-component="true" class="Label Label--success Label--large">Latest</span>')
        if rel["prerelease"]:
            labels.append('<span data-view-component="true" class="Label Label--warning Label--large">Pre-release</span>')
        body = f"""
          <div class="d-flex flex-column flex-md-row my-5 flex-justify-center">
            <div class="col-md-2 d-flex flex-md-column flex-row flex-wrap pr-md-6 mb-2 mb-md-0 flex-items-start pt-md-4">
              <div class="mb-2 f4 mr-1 mr-md-0 color-fg-muted">
                <local-time datetime="{rel['published']}" class="no-wrap" prefix="">{rel['published'][:10]}</local-time>
              </div>
              <div class="mr-3 mr-md-0 d-flex">
                <a href="/{slug}/tree/{tag_q}" data-view-component="true" class="Link Link--muted">
                  <svg aria-label="Tag" role="img" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-tag"><path d="M1 7.775V2.75C1"></path></svg>
                  <span class="ml-1 wb-break-all">{html.escape(rel['tag'])}</span>
                </a>
              </div>
              <div class="mb-md-2 mr-3 mr-md-0">
                <a class="Link Link--muted mb-2" href="/{slug}/commit/{rel['sha']}0000000000000000000000000000000">
                  <svg aria-label="Commit" role="img" height="16" viewBox="0 0 16 16" width="16" class="octicon octicon-git-commit"><path d="M11.93 8.5a4.002"></path></svg>
                  <code class="f5 ml-1 wb-break-all">{rel['sha']}</code>
                </a>
              </div>
            </div>
            <div class="col-md-9">
              <div class="Box">
                <div class="Box-body">
                  <div class="d-flex flex-md-row flex-column">
                    <div class="d-flex flex-row flex-1 mb-3 wb-break-word">
                      <div class="flex-1" data-pjax="#repo-content-pjax-container">
                        <span data-view-component="true" class="f1 text-bold d-inline mr-3"><a href="/{slug}/releases/tag/{tag_q}" data-view-component="true" class="Link--primary Link">{html.escape(rel['title'])}</a></span>
                        <div class="v-align-text-bottom ml-2 d-none d-md-inline">{''.join(labels)}</div>
                      </div>
                    </div>
                  </div>
                  <div data-pjax="true" data-test-selector="body-content" class="markdown-body my-3"><p>Release notes (fixture).</p></div>
                </div>
              </div>
            </div>
          </div>
"""
        if variant == "no_sections":
            parts.append(f'      <div class="release-entry">{body}      </div>\n')
        else:
            parts.append(f'      <section aria-labelledby="hd-{tag_q[:8]}">\n        <h2 class="sr-only" id="hd-{tag_q[:8]}">{html.escape(rel["title"])}</h2>{body}      </section>\n')
    if has_next:
        parts.append(f'      <div class="paginate-container d-none d-sm-flex flex-sm-justify-center"><div role="navigation" aria-label="Pagination" class="pagination"><span class="previous_page disabled">Previous</span> <em class="current">{page}</em> <a rel="next" class="next_page" href="/{slug}/releases?page={page + 1}">Next</a></div></div>\n')
    parts.append("    </div>\n  </main>\n</div>\n</body>\n</html>\n")
    return "".join(parts)


ROBOTS = """# If you would like to crawl GitHub contact us via https://support.github.com?tags=dotcom-robots
# We also provide an extensive API: https://docs.github.com
# (ФИКСТУРА: восстановлено по памяти; реальный файл скачивается при сборе)
User-agent: baidu
crawl-delay: 1


User-agent: *

Disallow: /*/*/pulse
Disallow: /*/*/projects
Disallow: /*/*/forks
Disallow: /*/*/issues/new
Disallow: /*/*/milestones/new
Disallow: /*/*/issues/search
Disallow: /*/*/commits/
Disallow: /*/*/branches
Disallow: /*/*/contributors
Disallow: /*/*/tags
Disallow: /*/*/stargazers
Disallow: /*/*/watchers
Disallow: /*/*/network
Disallow: /*/*/graphs
Disallow: /*/*/compare

Disallow: /*/tree/
Disallow: /gist/
Disallow: /*/download
Disallow: /*/revisions
Disallow: /*/commits/*?author
Disallow: /*/commits/*?path
Disallow: /*/comments
Disallow: /*/archive/
Disallow: /*/blame/
Disallow: /*/raw/
Disallow: /*/cache/
Disallow: /.git/
Disallow: */.git/
Disallow: /*.git$
Disallow: /search/advanced
Disallow: /search$
Disallow: /*q=
Disallow: /*.atom$

Disallow: /*source=*
Disallow: /*ref_cta=*
Disallow: /*plan=*
Disallow: /*return_to=*
Disallow: /*ref_loc=*
Disallow: /*setup_organization=*
Disallow: /*source_repo=*
Disallow: /*ref_page=*
Disallow: /*referrer=*
Disallow: /*report=*
Disallow: /*author=*
Disallow: /*since=*
Disallow: /*until=*
Disallow: /*commits?author=*
Disallow: /*report-abuse?report=*
Disallow: /*tab=*
Allow: /*?tab=achievements&achievement=*

Disallow: /account-login
"""


def main() -> None:
    (FIX / "api" / "repos").mkdir(parents=True, exist_ok=True)
    (FIX / "api" / "issues").mkdir(parents=True, exist_ok=True)
    (FIX / "html").mkdir(parents=True, exist_ok=True)
    variants = {"scrapy/parsel": "no_header_counters", "microsoft/playwright-python": "no_about",
                "encode/httpx": "languages_bar_only"}
    rel_variants = {"apache/arrow": "no_sections"}
    for r in REPOS:
        (slug, rid, created, lang, spdx, stars, forks, watchers, open_issues, size, homepage, desc, topics,
         rel_count, used_by, contrib, langs, tag_fmt, last_no) = r
        owner, name = slug.split("/")
        fname = f"{owner}__{name}"
        created_dt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        issues = gen_issues(slug, created_dt, last_no)
        rels = gen_releases(slug, tag_fmt, rel_count)
        repo_json = {
            "id": rid, "node_id": "R_kgDO" + hashlib.md5(slug.encode()).hexdigest()[:6], "name": name,
            "full_name": slug, "private": False,
            "owner": {"login": owner, "id": rng.randint(10_000, 9_999_999), "type": "Organization",
                      "avatar_url": f"https://avatars.githubusercontent.com/u/{rng.randint(1000, 999999)}?v=4"},
            "html_url": f"https://github.com/{slug}", "description": desc, "fork": False,
            "url": f"https://api.github.com/repos/{slug}",
            "created_at": created, "updated_at": iso(NOW - timedelta(hours=rng.uniform(0.2, 20))),
            "pushed_at": iso(NOW - timedelta(hours=rng.uniform(0.5, 60))),
            "homepage": homepage, "size": size, "stargazers_count": stars, "watchers_count": stars,
            "language": lang, "has_issues": True, "has_projects": True, "has_wiki": False,
            "forks_count": forks, "archived": False, "disabled": False, "open_issues_count": open_issues,
            "license": {"key": spdx.lower(), "name": "Other" if spdx == "NOASSERTION" else spdx,
                        "spdx_id": spdx, "url": None},
            "allow_forking": True, "is_template": False, "topics": topics, "visibility": "public",
            "forks": forks, "open_issues": open_issues, "watchers": stars, "default_branch": "main",
            "network_count": forks, "subscribers_count": watchers,
        }
        (FIX / "api" / "repos" / f"{fname}.json").write_text(
            json.dumps(repo_json, ensure_ascii=False, indent=1), encoding="utf-8")
        (FIX / "api" / "issues" / f"{fname}.json").write_text(
            json.dumps(issues, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (FIX / "html" / f"{fname}.html").write_text(repo_page(r, rels, variants.get(slug, "")), encoding="utf-8")
        for page in (1, 2):
            chunk = rels[(page - 1) * 10: page * 10]
            if chunk:
                (FIX / "html" / f"{fname}__releases_p{page}.html").write_text(
                    releases_page(r, chunk, page, page == 1 and len(rels) > 10, rel_variants.get(slug, "")),
                    encoding="utf-8")
        print(f"{slug:30} issues/PR: {len(issues):4d}  releases: {len(rels)}")
    (FIX / "robots.txt").write_text(ROBOTS, encoding="utf-8")


if __name__ == "__main__":
    main()
