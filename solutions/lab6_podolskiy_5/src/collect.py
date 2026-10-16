"""Повторный сбор исходных данных (средства лаб. 1 и 2) в сырой слой.

Источник 1 — GitHub REST API (JSON), как в лабораторной № 1:
    GET /repos/{owner}/{repo}                       — карточка репозитория
    GET /repos/{owner}/{repo}/issues?state=all      — issues и pull requests (Link-пагинация)
Источник 2 — HTML-страницы github.com (BeautifulSoup), как в лабораторной № 2:
    GET /{owner}/{repo}            — боковая колонка: About, Releases, Used by, Contributors, Languages
    GET /{owner}/{repo}/releases   — список релизов (события типа release)

Вежливость: честный User-Agent, проверка robots.txt (RFC 9309 + urllib.robotparser),
паузы между запросами, учёт X-RateLimit-* и Retry-After, таймауты, ограниченное
число повторов. Токен — только из переменной окружения GITHUB_TOKEN.

Запуск:
    python -m src.collect --repos config/repos.txt
    python -m src.collect --repos config/repos.txt --max-issue-pages 2 --since auto
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import config, parsers, rawio
from .robots import RobotsPolicy

TIMEOUT = (10, 30)          # (соединение, чтение), секунды
RETRIES = 3                 # всего попыток на запрос
MAX_RATE_WAIT = 90          # дольше ждать сброса лимита не будем — остановимся и сообщим
API_RESERVE = 2             # запас запросов, который не тратим


class RateLimitExhausted(RuntimeError):
    pass


def load_repo_list(path: Path) -> list[str]:
    repos = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            slug = parsers.repo_slug(line)
            if not slug:
                raise ValueError(f"Некорректная строка в {path}: {line!r}")
            repos.append(line.strip())
    return repos


class Fetcher:
    """HTTP-клиент с паузами, повторами и учётом лимитов. Ведёт статистику запросов."""

    def __init__(self, pause: float, headers: dict, name: str):
        self.session = requests.Session()
        self.session.headers.update(headers)
        self.pause = pause
        self.name = name
        self._last = 0.0
        self.stats = {"requests": 0, "bytes": 0, "status": {}, "retries": 0, "seconds": 0.0}
        self.rate = {}

    def _wait_turn(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.pause:
            time.sleep(self.pause - delta)

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, RETRIES + 1):
            self._wait_turn()
            t0 = time.perf_counter()
            try:
                resp = self.session.get(url, params=params, timeout=TIMEOUT)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                self.stats["retries"] += 1
                self._last = time.monotonic()
                if attempt < RETRIES:
                    time.sleep(2 ** attempt)
                continue
            finally:
                self.stats["seconds"] += time.perf_counter() - t0
            self._last = time.monotonic()
            self.stats["requests"] += 1
            self.stats["bytes"] += len(resp.content)
            code = str(resp.status_code)
            self.stats["status"][code] = self.stats["status"].get(code, 0) + 1
            self._remember_rate(resp)

            limited = resp.status_code == 429 or (resp.status_code == 403 and (
                resp.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in resp.headers))
            if limited:
                wait = self._limit_wait(resp)
                if wait > MAX_RATE_WAIT or attempt == RETRIES:
                    raise RateLimitExhausted(
                        f"{self.name}: лимит запросов исчерпан, повторите через ~{int(wait // 60) + 1} мин")
                print(f"  [{self.name}] лимит: пауза {wait:.0f} с")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                last_error = requests.HTTPError(f"HTTP {resp.status_code}")
                self.stats["retries"] += 1
                if attempt < RETRIES:
                    time.sleep(2 ** attempt)
                continue
            return resp
        raise RuntimeError(f"{self.name}: {url} не получен за {RETRIES} попыток") from last_error

    def _remember_rate(self, resp: requests.Response) -> None:
        for h in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                  "X-RateLimit-Used", "X-RateLimit-Resource"):
            if h in resp.headers:
                self.rate[h] = resp.headers[h]

    @staticmethod
    def _limit_wait(resp: requests.Response) -> float:
        if resp.headers.get("Retry-After", "").isdigit():
            return float(resp.headers["Retry-After"])
        reset = resp.headers.get("X-RateLimit-Reset", "")
        if reset.isdigit():
            return max(0.0, int(reset) - time.time()) + 1
        return 60.0


# ------------------------------------------------------------------ выборка полей
# Минимизация: из ответа API сохраняются только поля целевой схемы. Логины,
# аватары, тексты issues (body) и e-mail НЕ сохраняются — от автора остаются
# только роль (author_association) и признак бота.
def extract_repo(d: dict) -> dict:
    lic = d.get("license") or {}
    owner = d.get("owner") or {}
    return {
        "id": d.get("id"), "node_id": d.get("node_id"), "full_name": d.get("full_name"),
        "name": d.get("name"), "owner_login": owner.get("login"), "owner_type": owner.get("type"),
        "description": d.get("description"), "homepage": d.get("homepage"),
        "html_url": d.get("html_url"), "language": d.get("language"),
        "license_spdx": lic.get("spdx_id"), "topics": d.get("topics"),
        "stargazers_count": d.get("stargazers_count"), "forks_count": d.get("forks_count"),
        "subscribers_count": d.get("subscribers_count"), "open_issues_count": d.get("open_issues_count"),
        "size": d.get("size"), "default_branch": d.get("default_branch"),
        "archived": d.get("archived"), "disabled": d.get("disabled"), "fork": d.get("fork"),
        "has_issues": d.get("has_issues"),
        "created_at": d.get("created_at"), "updated_at": d.get("updated_at"), "pushed_at": d.get("pushed_at"),
    }


def extract_issue(d: dict) -> dict:
    user = d.get("user") or {}
    login = user.get("login") or ""
    pr = d.get("pull_request")
    reactions = d.get("reactions") or {}
    return {
        "id": d.get("id"), "node_id": d.get("node_id"), "number": d.get("number"),
        "title": d.get("title"), "state": d.get("state"), "state_reason": d.get("state_reason"),
        "locked": d.get("locked"), "comments": d.get("comments"),
        "created_at": d.get("created_at"), "updated_at": d.get("updated_at"), "closed_at": d.get("closed_at"),
        "html_url": d.get("html_url"), "repository_url": d.get("repository_url"),
        "author_association": d.get("author_association"),
        "user_is_bot": (user.get("type") == "Bot") or login.endswith("[bot]"),
        "labels": [{"name": lb.get("name"), "color": lb.get("color")}
                   for lb in (d.get("labels") or []) if isinstance(lb, dict)],
        "is_pull_request": pr is not None,          # /issues отдаёт и PR — маркируем
        "pr_merged_at": (pr or {}).get("merged_at"),
        "draft": d.get("draft"),
        "reactions_total": reactions.get("total_count"),
    }


def meta(source: str, url: str, status: int, parser_version: str, run_id: str, entity: str) -> dict:
    return {"_entity": entity, "_source": source, "_source_url": url,
            "_collected_at": rawio.utc_now_iso(), "_parser_version": parser_version,
            "_status": status, "_run_id": run_id}


# ------------------------------------------------------------------ API
def last_updated_by_repo() -> dict[str, str]:
    """Для --since auto: максимальный updated_at по каждому репозиторию в сыром слое."""
    best: dict[str, str] = {}
    for rec in rawio.read_jsonl(config.RAW_API):
        if rec.get("_entity") != "issue" or not rec.get("updated_at"):
            continue
        slug = parsers.repo_slug(rec.get("repo_slug_requested"))
        if slug and rec["updated_at"] > best.get(slug, ""):
            best[slug] = rec["updated_at"]
    return best


def collect_api(repos: list[str], args, run_id: str, summary: dict) -> None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token and not args.no_token:
        headers["Authorization"] = f"Bearer {token}"
    print(f"API: {config.GITHUB_API_URL} | токен: {config.mask(token) if 'Authorization' in headers else '<нет>'}")
    api = Fetcher(args.api_pause, headers, "api")

    # /rate_limit не расходует лимит — планируем бюджет до начала сбора
    rl = api.get(f"{config.GITHUB_API_URL}/rate_limit")
    remaining = None
    if rl.status_code == 200:
        core = rl.json().get("resources", {}).get("core", {})
        remaining = core.get("remaining")
        print(f"Лимит API: {remaining} из {core.get('limit')} запросов в час")
    pages = args.max_issue_pages
    need = len(repos) * (1 + pages)
    if remaining is not None and remaining - API_RESERVE < need:
        pages = max(0, (remaining - API_RESERVE) // max(1, len(repos)) - 1)
        print(f"⚠ Бюджета не хватает на {need} запросов: страниц issues на репозиторий → {pages}")
    summary["api_plan"] = {"rate_remaining_before": remaining, "planned_requests": len(repos) * (1 + pages),
                           "issue_pages_per_repo": pages}

    since_map = last_updated_by_repo() if args.since == "auto" else {}
    for slug in repos:
        owner, name = slug.split("/")
        url = f"{config.GITHUB_API_URL}/repos/{owner}/{name}"
        batch: list[dict] = []
        resp = api.get(url)
        m = meta(config.SOURCE_API, resp.url, resp.status_code, config.API_PARSER_VERSION, run_id, "repo")
        if resp.status_code == 200:
            batch.append({**extract_repo(resp.json()), "repo_slug_requested": slug, **m})
        else:
            batch.append({"repo_slug_requested": slug, "_error": resp.text[:300], **m})
            print(f"  {slug}: HTTP {resp.status_code}")
            rawio.append_records(config.RAW_API, batch, run_id)
            continue

        params = {"state": "all", "per_page": args.per_page, "sort": "updated", "direction": "desc"}
        since = args.since if args.since not in (None, "", "auto", "none") else since_map.get(slug.lower())
        if since:
            params["since"] = since
        next_url, page_no, n_items = f"{url}/issues", 0, 0
        while next_url and page_no < pages:           # предохранитель: не больше pages страниц
            resp = api.get(next_url, params=params if page_no == 0 else None)
            page_no += 1
            m = meta(config.SOURCE_API, resp.url, resp.status_code, config.API_PARSER_VERSION, run_id, "issue")
            if resp.status_code != 200:
                batch.append({"repo_slug_requested": slug, "_error": resp.text[:300], **m})
                break
            items = resp.json()
            for it in items:
                batch.append({**extract_issue(it), "repo_slug_requested": slug, **m})
            n_items += len(items)
            next_url = resp.links.get("next", {}).get("url")   # Link: <…>; rel="next"
        rawio.append_records(config.RAW_API, batch, run_id)
        print(f"  {slug}: репозиторий + {n_items} issues/PR ({page_no} стр.)"
              + (f", since={since}" if since else ""))
    summary["api"] = {**api.stats, "rate_after": api.rate}


# ------------------------------------------------------------------ HTML
def collect_html(repos: list[str], args, run_id: str, summary: dict) -> None:
    web = Fetcher(args.html_pause, {"User-Agent": config.USER_AGENT, "Accept": "text/html",
                                    "Accept-Language": "en-US,en;q=0.8"}, "html")
    robots_url = f"{config.GITHUB_WEB_URL}/robots.txt"
    try:
        r = web.get(robots_url)
        policy = RobotsPolicy(robots_url, r.status_code, r.text if r.ok else "", config.USER_AGENT)
    except RuntimeError as exc:
        print(f"robots.txt недоступен: {exc}")
        policy = RobotsPolicy(robots_url, None, "", config.USER_AGENT)
    if policy.crawl_delay:
        web.pause = max(web.pause, policy.crawl_delay)
    print(f"robots.txt: статус {policy.status}, группа «{policy.group_name}», правил {len(policy.rules)}, "
          f"crawl-delay {policy.crawl_delay}; пауза между страницами {web.pause} с")

    decisions = []
    # Контрольные адреса — документируем, что запрещённые разделы мы не трогаем
    if repos:
        o, n = repos[0].split("/")
        for probe in (f"/{o}/{n}/contributors", f"/{o}/{n}/stargazers", f"/{o}/{n}/network/dependents",
                      f"/{o}/{n}/tree/main", f"/{o}/{n}?tab=readme-ov-file"):
            decisions.append({**policy.check(config.GITHUB_WEB_URL + probe), "purpose": "контроль (не запрашивается)"})

    pages_dir = config.RAW_HTML_PAGES / run_id
    for slug in repos:
        o, n = slug.split("/")
        targets = [("repo_page", f"{config.GITHUB_WEB_URL}/{o}/{n}")]
        if args.release_pages > 0:
            targets.append(("release", f"{config.GITHUB_WEB_URL}/{o}/{n}/releases"))
        batch: list[dict] = []
        for kind, url in targets:
            pages_left = args.release_pages if kind == "release" else 1
            while url and pages_left > 0:
                pages_left -= 1
                verdict = policy.check(url)
                decisions.append({**verdict, "purpose": kind})
                if not verdict["allowed"]:
                    print(f"  {slug}: {url} запрещён robots.txt ({verdict['reason']}) — пропуск")
                    break
                resp = web.get(url)
                m = meta(config.SOURCE_HTML, resp.url, resp.status_code, config.HTML_PARSER_VERSION, run_id, kind)
                m["_final_url"] = resp.url
                if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", "html"):
                    batch.append({"repo_slug_requested": slug, "_error": f"HTTP {resp.status_code}", **m})
                    break
                if args.save_html:
                    pages_dir.mkdir(parents=True, exist_ok=True)
                    fname = f"{o}__{n}__{kind}__{datetime.now(timezone.utc):%H%M%S%f}.html.gz"
                    with gzip.open(pages_dir / fname, "wt", encoding="utf-8") as fh:
                        fh.write(resp.text)
                    m["_html_file"] = f"html/{run_id}/{fname}"
                if kind == "repo_page":
                    batch.append({**parsers.parse_repo_page(resp.text), "repo_slug_requested": slug, **m})
                    url = None
                else:
                    rels, nxt = parsers.parse_releases_page(resp.text)
                    batch.extend({**rel, "repo_slug_requested": slug, **m} for rel in rels)
                    url = requests.compat.urljoin(resp.url, nxt) if nxt else None
        rawio.append_records(config.RAW_HTML, batch, run_id)
        n_rel = sum(1 for b in batch if b["_entity"] == "release" and "_error" not in b)
        print(f"  {slug}: страница репозитория + {n_rel} релизов")

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    rob_path = config.LOG_DIR / f"robots_decisions_{run_id}.json"
    rob_path.write_text(json.dumps({"robots_url": robots_url, "status": policy.status,
                                    "group": policy.group_name, "crawl_delay": policy.crawl_delay,
                                    "decisions": decisions}, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["html"] = {**web.stats, "robots_status": policy.status, "robots_file": rob_path.name,
                       "disallowed_requests": sum(1 for d in decisions if d["purpose"] != "контроль (не запрашивается)"
                                                  and not d["allowed"])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Сбор GitHub API + HTML в сырой слой (только дозапись)")
    ap.add_argument("--repos", type=Path, default=config.PROJECT_ROOT / "config" / "repos.txt")
    ap.add_argument("--max-issue-pages", type=int, default=3, help="страниц /issues на репозиторий (по 100)")
    ap.add_argument("--per-page", type=int, default=100)
    ap.add_argument("--release-pages", type=int, default=1, help="страниц /releases (по 10 релизов)")
    ap.add_argument("--since", default="none", help="none | auto (инкремент по raw) | ISO 8601")
    ap.add_argument("--api-pause", type=float, default=1.0)
    ap.add_argument("--html-pause", type=float, default=3.0)
    ap.add_argument("--no-api", action="store_true")
    ap.add_argument("--no-html", action="store_true")
    ap.add_argument("--no-token", action="store_true", help="не передавать GITHUB_TOKEN")
    ap.add_argument("--save-html", action=argparse.BooleanOptionalAction, default=True,
                    help="сохранять полученные HTML (gzip) в raw/html/<run_id>/ для перепарсинга")
    args = ap.parse_args(argv)

    config.ensure_dirs()
    rawio.verify_all()                     # сырой слой не должен быть изменён «вручную»
    repos = load_repo_list(args.repos)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"Запуск {run_id}: репозиториев {len(repos)}")
    summary: dict = {"run_id": run_id, "repos": repos, "started_at": rawio.utc_now_iso(),
                     "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}}
    t0 = time.perf_counter()
    exit_code = 0
    try:
        if not args.no_api:
            collect_api(repos, args, run_id, summary)
        if not args.no_html:
            collect_html(repos, args, run_id, summary)
    except RateLimitExhausted as exc:
        print(f"⚠ {exc}. Уже собранное сохранено в raw/.")
        summary["stopped"] = str(exc)
        exit_code = 3
    summary["finished_at"] = rawio.utc_now_iso()
    summary["duration_s"] = round(time.perf_counter() - t0, 1)
    out = config.LOG_DIR / f"collect_{run_id}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Готово за {summary['duration_s']} с; сводка: {out.relative_to(config.DATA_ROOT)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
