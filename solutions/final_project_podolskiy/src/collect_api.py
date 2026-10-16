"""Сбор API-источника: GitHub REST API — обогащение репозиториев из HTML-слоя метриками.

Связь источников — по бизнес-ключу owner/repo (в нижнем регистре: GitHub сравнивает имена без учёта регистра).

Снижение нагрузки:
  1) водяной знак (state/watermark.json): репозиторий запрашивается, только если он новый, если дата
     «Updated» на странице темы новее сохранённых pushed_at/updated_at или если с прошлой проверки прошло
     >= refresh_max_age_hours;
  2) условные запросы If-None-Match (state/etags.json): неизменившийся репозиторий -> 304 без тела;
     по документации GitHub ответ 304 на авторизованный запрос не расходует основную квоту —
     это проверяется по заголовку X-RateLimit-Used в отчёте о прогоне (api_quota.used_delta_by_status);
  3) без токена (60 запр/ч) — режим search: /search/repositories по темам, 100 репозиториев на запрос.

Минимизация данных: из ответа сохраняются только нужные поля; у владельца — login и type
(аватары, URL профиля и прочие поля не сохраняются).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode

from http_client import PoliteClient
from observability import RunContext
from settings import iso, parse_iso, utcnow
from collect_html import write_raw

SOURCE = "github_api_repos"
KEEP_FIELDS = ["id", "name", "full_name", "description", "fork", "created_at", "updated_at", "pushed_at",
               "homepage", "size", "stargazers_count", "watchers_count", "language", "forks_count",
               "open_issues_count", "archived", "disabled", "topics", "visibility", "default_branch",
               "subscribers_count", "network_count", "has_issues", "has_wiki", "has_discussions", "is_template"]


def minimize(repo: dict) -> dict:
    out = {k: repo.get(k) for k in KEEP_FIELDS if k in repo}
    owner = repo.get("owner") or {}
    out["owner"] = {"login": owner.get("login"), "type": owner.get("type")}
    lic = repo.get("license")
    out["license"] = {"key": lic.get("key"), "spdx_id": lic.get("spdx_id"), "name": lic.get("name")} if lic else None
    return out


def resolve_mode(cfg: dict, token: str | None) -> str:
    mode = cfg["sources"]["api"].get("mode", "auto")
    if mode == "auto":
        return "rest" if token else "search"
    return mode


def api_headers(cfg: dict, token: str | None) -> dict:
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": cfg["sources"]["api"]["api_version"]}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def select_keys(listing_index: dict[str, dict], state: dict, refresh_max_age_hours: float,
                force_keys: set[str] | None = None) -> tuple[list[tuple[str, str, str]], dict]:
    """Какие репозитории запрашивать в API. Возвращает [(key, full_name, причина)] и счётчики причин."""
    now = utcnow()
    repos_state = state.setdefault("repos", {})
    selected, reasons = [], {"new": 0, "html_newer": 0, "stale": 0, "forced": 0, "skipped_watermark": 0}
    for key, info in sorted(listing_index.items()):
        st = repos_state.get(key)
        full = info["full_name"]
        if force_keys and key in force_keys:
            selected.append((key, full, "forced")); reasons["forced"] += 1
            continue
        if not st or not st.get("checked_at"):
            selected.append((key, full, "new")); reasons["new"] += 1
            continue
        html_upd = parse_iso(info.get("html_updated_at"))
        marks = [parse_iso(st.get(k)) for k in ("pushed_at", "gh_updated_at")]
        marks = [m for m in marks if m]
        if html_upd and marks and html_upd > max(marks):
            selected.append((key, full, "html_newer")); reasons["html_newer"] += 1
            continue
        checked = parse_iso(st["checked_at"])
        if checked is None or now - checked >= timedelta(hours=refresh_max_age_hours):
            selected.append((key, full, "stale")); reasons["stale"] += 1
            continue
        reasons["skipped_watermark"] += 1
    return selected, reasons


def _remember(state: dict, key: str, repo: dict | None, status: int | None) -> None:
    st = state.setdefault("repos", {}).setdefault(key, {})
    st["checked_at"] = iso(utcnow())
    st["status"] = status
    if repo:
        st["pushed_at"] = repo.get("pushed_at")
        st["gh_updated_at"] = repo.get("updated_at")
        st["repo_id"] = repo.get("id")
        if repo.get("updated_at") and (not state.get("api_max_updated_at")
                                       or repo["updated_at"] > state["api_max_updated_at"]):
            state["api_max_updated_at"] = repo["updated_at"]


def _quota_low(res, cfg: dict, token: str | None) -> bool:
    if res.response is None:
        return False
    rem = res.response.headers.get("X-RateLimit-Remaining")
    if rem is None or not rem.isdigit():
        return False
    if (res.response.headers.get("X-RateLimit-Resource") or "core") != "core":
        return False
    reserve = int(cfg["sources"]["api"]["quota_reserve"]) if token else 60 - int(cfg["sources"]["api"]["unauth_core_budget"])
    return int(rem) <= reserve


def collect_repos(client: PoliteClient, cfg: dict, ctx: RunContext, listing_index: dict[str, dict],
                  state: dict, checkpoint: dict, token: str | None, topics: list[str],
                  refresh_max_age_hours: float | None = None, save_checkpoint=None) -> dict:
    api = cfg["sources"]["api"]
    base = api["base_url"].rstrip("/")
    mode = resolve_mode(cfg, token)
    headers = api_headers(cfg, token)
    max_age = api["refresh_max_age_hours"] if refresh_max_age_hours is None else refresh_max_age_hours
    done: set[str] = set(checkpoint.setdefault("api_done", []))
    every = int(cfg["resume"]["checkpoint_every_api_keys"])

    selected, reasons = select_keys(listing_index, state, max_age)
    resumed = [k for k, _, _ in selected if k in done]
    todo = [(k, f, r) for k, f, r in selected if k not in done]
    stats: dict[str, Any] = {"api_mode": mode, "api_selected": len(selected), "api_reasons": reasons,
                             "api_skipped_watermark": reasons["skipped_watermark"],
                             "api_resumed_skip": len(resumed), "api_200": 0, "api_304": 0, "api_404": 0,
                             "api_failed": 0, "api_skipped_breaker": 0, "api_stopped_quota": False,
                             "api_source_unavailable": False, "verified_keys": [], "api_search_requests": 0}
    ctx.log("api_selection", mode=mode, selected=len(selected), resumed_skip=len(resumed), **reasons)
    ctx.say(f"  [API] режим {mode}; к запросу {len(todo)} (новые {reasons['new']}, изменились по HTML "
            f"{reasons['html_newer']}, устарели {reasons['stale']}), пропущено по водяному знаку "
            f"{reasons['skipped_watermark']}, уже обработано до обрыва {len(resumed)}")

    def mark_done(key: str) -> None:
        done.add(key)
        checkpoint["api_done"] = sorted(done)
        if save_checkpoint and len(done) % every == 0:
            save_checkpoint()

    # ---- режим search (без токена): пакетное обогащение по темам
    if mode == "search" and todo:
        need = {k: (f, r) for k, f, r in todo}
        for topic in topics:
            if not need:
                break
            q = f"topic:{topic} language:{cfg['sources']['html'].get('language_filter') or 'python'}"
            url = f"{base}/search/repositories?" + urlencode({"q": q, "sort": "stars", "order": "desc",
                                                               "per_page": 100})
            pages = 0
            while url and pages < 10 and need:
                res = client.get(url, source="github_api_search", limiter_key="api_search",
                                 conditional=False, headers=headers, robots_check=False)
                stats["api_search_requests"] += 1
                pages += 1
                if not res.ok or res.response is None:
                    stats["api_failed"] += 1
                    if res.skipped == "breaker_open":
                        stats["api_source_unavailable"] = True
                    break
                items = res.response.json().get("items", [])
                hits = []
                for item in items:
                    key = (item.get("full_name") or "").lower()
                    if key in need:
                        hits.append(minimize(item))
                        _remember(state, key, item, 200)
                        stats["verified_keys"].append(key)
                        need.pop(key)
                        mark_done(key)
                if hits:
                    write_raw(hits, SOURCE, ctx, {"_url": url, "_status": res.status, "_endpoint": "search",
                                                  "_parser_version": cfg["project"]["api_normalizer_version"]})
                    stats["api_200"] += len(hits)
                url = res.response.links.get("next", {}).get("url")
        todo = [(k, f, r) for k, f, r in todo if k in need]
        budget = int(api["unauth_core_budget"])
        if len(todo) > budget:
            ctx.log("api_budget_limited", level="WARNING", left_for_next_run=len(todo) - budget)
            todo = todo[:budget]

    # ---- режим rest: поштучно /repos/{owner}/{repo} с ETag
    for i, (key, full, reason) in enumerate(todo):
        owner, name = full.split("/", 1)
        url = f"{base}/repos/{quote(owner)}/{quote(name)}"
        res = client.get(url, source=SOURCE, limiter_key="api", conditional=bool(api.get("use_etag", True)),
                         headers=headers, robots_check=False)
        if res.skipped == "breaker_open" or res.skipped == "wait_too_long":
            left = len(todo) - i
            stats["api_skipped_breaker"] += left
            stats["api_source_unavailable"] = True
            ctx.log("api_source_isolated", level="ERROR", skipped_keys=left, reason=res.skipped)
            break
        if res.not_modified:
            stats["api_304"] += 1
            _remember(state, key, None, 304)
            stats["verified_keys"].append(key)
        elif res.ok and res.response is not None:
            repo = res.response.json()
            payload = minimize(repo)
            write_raw([payload], SOURCE, ctx, {
                "_url": url, "_status": res.status, "_endpoint": "repos", "requested_key": key,
                "_etag": res.response.headers.get("ETag"), "_reason": reason,
                "_parser_version": cfg["project"]["api_normalizer_version"]})
            _remember(state, key, repo, 200)
            if (repo.get("full_name") or "").lower() != key:
                _remember(state, (repo.get("full_name") or "").lower(), repo, 200)
                ctx.log("repo_renamed", requested=key, actual=repo.get("full_name"))
            stats["api_200"] += 1
            stats["verified_keys"].append(key)
        elif res.status == 404:
            stats["api_404"] += 1
            _remember(state, key, None, 404)
            ctx.log("repo_not_found", level="WARNING", key=key)
        elif res.status == 401:
            ctx.incident("auth", "GitHub API ответил 401: токен недействителен или отозван — сбор API остановлен",
                         source=SOURCE)
            stats["api_failed"] += 1
            break
        else:
            stats["api_failed"] += 1                       # временный сбой: ключ НЕ отмечаем, повторим позже
            continue
        mark_done(key)
        if _quota_low(res, cfg, token):
            stats["api_stopped_quota"] = True
            ctx.log("api_quota_guard", level="WARNING", remaining=res.response.headers.get("X-RateLimit-Remaining"),
                    left_for_next_run=len(todo) - i - 1)
            ctx.say("  [API] остаток квоты достиг резерва — оставшиеся репозитории перенесены на следующий прогон")
            break
        if (i + 1) % 50 == 0:
            ctx.say(f"  [API] обработано {i + 1}/{len(todo)} (200: {stats['api_200']}, 304: {stats['api_304']})")
    client.etags.save()
    if save_checkpoint:
        save_checkpoint()
    return stats
