"""Сбор HTML-источника: страницы тем GitHub github.com/topics/<topic>?l=python&page=N.

Инструмент — requests + BeautifulSoup: диагностика (python src/collect_html.py --diagnose) показывает, что
карточки репозиториев приходят в исходном HTML, а «Load more…» — обычный GET той же страницы с page=N+1,
то есть браузер (Playwright) не нужен.

Парсер устойчив к смене вёрстки: три стратегии по убыванию полноты
  1) article-cards  — карточки <article> (владелец/репозиторий, звёзды, описание, теги, язык, дата);
  2) embedded-json  — если страница станет React-приложением: JSON в <script type="application/json">;
  3) h3-links       — последний рубеж: только ключи owner/repo из заголовков.
Разметка воспроизведена по знанию вёрстки github.com/topics и ДОЛЖНА быть подтверждена первым реальным
прогоном (см. README, раздел «Риски»): при 0 карточек на полученной странице срабатывает красное оповещение.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_client import PoliteClient                      # noqa: E402
from observability import RunContext                       # noqa: E402
from settings import DIRS, HTML_SNAPSHOTS_DIR, iso, load_config, parse_iso, utcnow  # noqa: E402

SOURCE = "github_topics_html"
REPO_HREF = re.compile(r"^/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})/?$")
RESERVED_OWNERS = {"topics", "login", "logout", "join", "features", "orgs", "sponsors", "marketplace",
                   "collections", "about", "pricing", "search", "settings", "apps", "site", "explore",
                   "trending", "enterprise", "team", "customer-stories", "readme", "security", "events"}
STAR_ARIA = re.compile(r"([\d,\s]+)\s+users?\s+starred", re.I)
UPDATED_TEXT_RX = re.compile(r"Updated\s+(?:on\s+)?([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})")
STAR_TEXT_RX = re.compile(r"\bStar\s+(\d[\d.,]*\s*[kKmM]?)\b")
TOTAL_RX = re.compile(r"Here\s+(?:are|is)\s+([\d,]+)\s+public\s+repositor", re.I)
BLOCK_MARKERS = ["captcha", "are you human", "access denied", "unusual traffic", "whoa there",
                 "abuse detection", "too many requests", "please verify you are a human",
                 "доступ ограничен", "подтвердите, что вы не робот"]


# ------------------------------------------------------------------ разбор значений

def parse_count(text: str | None) -> tuple[int | None, bool]:
    """'71.3k' -> (71300, True); '1,234' -> (1234, False); '2.1m' -> (2100000, True)."""
    if text is None:
        return None, False
    t = text.strip().lower().replace(" ", "").replace(" ", "")
    if not t:
        return None, False
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km])", t)
    if m:
        mult = 1_000 if m.group(2) == "k" else 1_000_000
        return int(round(float(m.group(1)) * mult)), True
    digits = t.replace(",", "")
    if digits.isdigit():
        return int(digits), False
    return None, False


def _text(el) -> str | None:
    if el is None:
        return None
    s = " ".join(el.get_text(" ", strip=True).split())
    return s or None


def _repo_key_from_href(href: str | None) -> tuple[str, str] | None:
    if not href:
        return None
    m = REPO_HREF.match(href.split("?")[0].split("#")[0])
    if not m or m.group(1).lower() in RESERVED_OWNERS:
        return None
    return m.group(1), m.group(2)


def _stars_from_card(card) -> tuple[int | None, bool, str | None]:
    """Звёзды: сначала точное значение (aria-label / title), иначе сокращённый текст '71.3k'."""
    el = card.select_one("#repo-stars-counter-star, span.js-social-count, [aria-label*='starred']")
    if el is not None:
        aria = el.get("aria-label") or ""
        m = STAR_ARIA.search(aria)
        if m:
            val, _ = parse_count(m.group(1).replace(" ", ""))
            if val is not None:
                return val, False, aria
        title = el.get("title")
        val, approx = parse_count(title)
        if val is not None:
            return val, approx, title
        val, approx = parse_count(el.get_text(strip=True))
        return val, approx, el.get_text(strip=True)
    # фолбэк: ссылка на stargazers с числом в тексте
    a = card.select_one("a[href$='/stargazers']")
    if a is not None:
        val, approx = parse_count(a.get_text(strip=True))
        return val, approx, a.get_text(strip=True)
    return None, False, None


# ------------------------------------------------------------------ стратегии разбора

def _parse_article_cards(soup: BeautifulSoup) -> list[dict]:
    cards = []
    for art in soup.select("article"):
        key = None
        for a in art.select("h3 a[href]") or art.select("a[href]"):
            key = _repo_key_from_href(a.get("href"))
            if key:
                break
        if not key:
            continue
        stars, approx, stars_raw = _stars_from_card(art)
        desc = None
        for p in art.select("p"):
            if p.find_parent("h3") is None:
                desc = _text(p)
                if desc:
                    break
        tags = [t for t in (_text(a) for a in art.select("a.topic-tag, a[href^='/topics/']")) if t]
        lang = _text(art.select_one("[itemprop='programmingLanguage']"))
        rt = art.select_one("relative-time[datetime], time[datetime]")
        updated = rt.get("datetime") if rt is not None else None
        card_text = art.get_text(" ", strip=True)
        if updated is None:                       # фолбэк: «Updated Jul 25, 2026» без атрибута datetime
            m = UPDATED_TEXT_RX.search(card_text)
            if m:
                try:
                    updated = datetime.strptime(m.group(1), "%b %d, %Y").replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    updated = None
        if stars is None:                         # фолбэк: текст кнопки «Star 71.3k»
            m = STAR_TEXT_RX.search(card_text)
            if m:
                stars, approx = parse_count(m.group(1))
                stars_raw = m.group(0)
        cards.append({
            "owner": key[0], "repo": key[1], "full_name": f"{key[0]}/{key[1]}",
            "stars_raw": stars_raw, "stars": stars, "stars_approx": approx,
            "description": desc, "tags": list(dict.fromkeys(tags)), "language": lang,
            "updated_raw": updated,
        })
    return cards


def _walk_json(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_json(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_json(v)


def _parse_embedded_json(soup: BeautifulSoup) -> list[dict]:
    """Фолбэк на случай перевода страницы на React: ищем объекты репозиториев во встроенном JSON."""
    found: dict[str, dict] = {}
    for sc in soup.select("script[type='application/json'], script#__NEXT_DATA__"):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk_json(data):
            full = obj.get("full_name") or obj.get("nameWithOwner")
            if not full and isinstance(obj.get("owner"), (dict, str)) and obj.get("name"):
                owner = obj["owner"].get("login") if isinstance(obj["owner"], dict) else obj["owner"]
                full = f"{owner}/{obj['name']}" if owner else None
            if not full or not _repo_key_from_href("/" + str(full)):
                continue
            stars = obj.get("stargazers_count", obj.get("stargazerCount", obj.get("stars")))
            lang = obj.get("language")
            if isinstance(lang, dict):
                lang = lang.get("name")
            owner, repo = str(full).split("/", 1)
            found.setdefault(str(full).lower(), {
                "owner": owner, "repo": repo, "full_name": str(full),
                "stars_raw": None if stars is None else str(stars),
                "stars": stars if isinstance(stars, int) else parse_count(str(stars))[0] if stars else None,
                "stars_approx": False, "description": obj.get("description"),
                "tags": [t if isinstance(t, str) else t.get("name") for t in (obj.get("topics") or [])
                         if isinstance(t, (str, dict))],
                "language": lang, "updated_raw": obj.get("pushed_at") or obj.get("updated_at"),
            })
    return list(found.values())


def _parse_h3_links(soup: BeautifulSoup) -> list[dict]:
    out: dict[str, dict] = {}
    for a in soup.select("h3 a[href]"):
        key = _repo_key_from_href(a.get("href"))
        if key:
            full = f"{key[0]}/{key[1]}"
            out.setdefault(full.lower(), {"owner": key[0], "repo": key[1], "full_name": full,
                                          "stars_raw": None, "stars": None, "stars_approx": False,
                                          "description": None, "tags": [], "language": None,
                                          "updated_raw": None})
    return list(out.values())


def parse_topic_page(html: str, topic: str, page: int) -> tuple[list[dict], dict]:
    """Разбор одной страницы темы. Возвращает (карточки, метаданные страницы)."""
    soup = BeautifulSoup(html, "lxml")
    strategy = "article-cards"
    cards = _parse_article_cards(soup)
    if not cards:
        strategy, cards = "embedded-json", _parse_embedded_json(soup)
    if not cards:
        strategy, cards = "h3-links", _parse_h3_links(soup)
    if not cards:
        strategy = "none"

    text = soup.get_text(" ", strip=True)
    m = TOTAL_RX.search(text)
    next_page = page + 1
    has_next = False
    for el in soup.select("form[action], a[href]"):
        target = el.get("action") or el.get("href") or ""
        if f"/topics/{topic}" in target and re.search(rf"[?&]page={next_page}\b", target):
            has_next = True
            break
    if not has_next:
        has_next = bool(soup.find(string=re.compile(r"Load more", re.I)))
    meta = {"strategy": strategy, "cards": len(cards), "has_next": has_next,
            "topic_total": int(m.group(1).replace(",", "")) if m else None,
            "h1": _text(soup.select_one("h1"))}
    return cards, meta


def detect_block(status: int | None, html: str, cards: int, min_bytes: int = 5000) -> tuple[str, list[str]]:
    """Детектор блокировки и «тихой» деградации: код, маркеры в теле, размер, 200 без карточек."""
    body = (html or "").lower()
    signals = []
    if status in (403, 429):
        signals.append(f"код {status}")
    found = [mk for mk in BLOCK_MARKERS if mk in body]
    if found and cards == 0:
        signals.append("маркеры: " + ", ".join(found[:3]))
    if html is not None and len(html) < min_bytes:
        signals.append(f"подозрительно маленький ответ ({len(html)} символов)")
    if status == 200 and cards == 0:
        signals.append("нет ожидаемых карточек при коде 200")
    blocked = status in (403, 429) or (bool(found) and cards == 0)
    return ("блокировка" if blocked else "подозрение" if signals else "ok"), signals


# ------------------------------------------------------------------ сырой слой

def topic_url(base: str, topic: str, page: int, language: str | None, sort_mode: str = "stars") -> str:
    params: dict[str, Any] = {}
    if language:
        params["l"] = language
    if sort_mode == "updated":
        params.update({"o": "desc", "s": "updated"})
    if page > 1:
        params["page"] = page
    return f"{base.rstrip('/')}/topics/{topic}" + (f"?{urlencode(params)}" if params else "")


def write_raw(records: list[dict], source: str, ctx: RunContext, provenance: dict) -> int:
    """Сырой слой: JSON Lines, ТОЛЬКО дозапись, с метаданными происхождения на каждой строке."""
    path = DIRS["raw"] / f"{source}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = iso(utcnow())
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            env = {"_run_id": ctx.run_id, "_source": source, "_collected_at": now, **provenance, "payload": rec}
            f.write(json.dumps(env, ensure_ascii=False, default=str) + "\n")
    return len(records)


def read_raw(source: str, run_id: str | None = None) -> list[dict]:
    path = DIRS["raw"] / f"{source}.jsonl"
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            env = json.loads(line)
            if run_id is None or env.get("_run_id") == run_id:
                out.append(env)
    return out


def save_html_snapshot(html: str, topic: str, page: int, ctx: RunContext) -> str:
    """Страница целиком (gzip) — для перепарсинга без повторного обращения к сайту и для тестов."""
    d = HTML_SNAPSHOTS_DIR / topic
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"p{page:02d}_{ctx.run_id}.html.gz"
    p.write_bytes(gzip.compress(html.encode("utf-8")))
    return f"raw/html_pages/{topic}/{p.name}"


# ------------------------------------------------------------------ сбор

def collect_topics(client: PoliteClient, cfg: dict, ctx: RunContext, checkpoint: dict,
                   topics: list[str] | None = None, max_pages: int | None = None,
                   save_checkpoint=None) -> dict:
    """Обход страниц тем с пагинацией и остановкой. Возвращает статистику и список карточек прогона."""
    src = cfg["sources"]["html"]
    base = src["base_url"]
    parser_version = cfg["project"]["parser_version"]
    topics = topics or src["topics"]
    max_pages = max_pages or int(src["max_pages_per_topic"])
    page_size = int(src["expected_cards_per_page"])
    done: dict = checkpoint.setdefault("html_done", {})     # {"topic|sort": {"<page>": run_id}}
    resume_cache: dict[str, list] = {}

    def resumed_records(run_id: str) -> list[dict]:
        if run_id not in resume_cache:
            resume_cache[run_id] = read_raw(SOURCE, run_id)
        return resume_cache[run_id]

    stats = {"html_pages_ok": 0, "html_pages_failed": 0, "html_cards": 0, "html_pages_resumed": 0,
             "html_pages_skipped_robots": 0, "html_blocked": 0, "topics": {}}
    listings: list[dict] = []

    for sort_mode in src.get("sort_modes", ["stars"]):
        for topic in topics:
            tstat = stats["topics"].setdefault(topic, {"pages": 0, "cards": 0, "strategy": None, "total": None})
            rank = 0
            for page in range(1, max_pages + 1):
                dkey = f"{topic}|{sort_mode}"
                done_run = done.get(dkey, {}).get(str(page))
                if done_run:
                    # возобновление: страница уже собрана прерванным прогоном — берём из сырого слоя
                    prev = [e for e in resumed_records(done_run)
                            if e["page"] == page and e["topic"] == topic and e.get("sort_mode", "stars") == sort_mode]
                    for e in prev:
                        listings.append(e)
                        rank = max(rank, e["payload"].get("rank_in_topic", 0))
                    stats["html_pages_resumed"] += 1
                    stats["html_cards"] += len(prev)
                    stats["html_cards_resumed"] = stats.get("html_cards_resumed", 0) + len(prev)
                    ctx.log("html_page_resumed", topic=topic, page=page, cards=len(prev))
                    if prev and len(prev) < page_size:
                        break
                    continue
                url = topic_url(base, topic, page, src.get("language_filter"), sort_mode)
                res = client.get(url, source=SOURCE, limiter_key="html", conditional=True, cacheable=True,
                                 headers={"Accept": "text/html,application/xhtml+xml"})
                if res.skipped:
                    if res.skipped == "robots":
                        stats["html_pages_skipped_robots"] += 1
                    else:
                        stats["html_pages_failed"] += 1
                    break
                if res.not_modified:
                    # ETag страницы совпал: используем последний снимок из сырого слоя
                    prev = [e for e in read_raw(SOURCE) if e["topic"] == topic and e["page"] == page]
                    last_run = prev[-1]["_run_id"] if prev else None
                    listings.extend(e for e in prev if e["_run_id"] == last_run)
                    stats["html_pages_ok"] += 1
                    continue
                if res.status == 404 and page > 1:
                    # страницы N нет: естественный конец пагинации, а не отказ источника
                    ctx.log("html_pagination_end", topic=topic, page=page)
                    break
                if not res.ok or res.response is None:
                    stats["html_pages_failed"] += 1
                    ctx.log("html_page_failed", level="ERROR", topic=topic, page=page,
                            status=res.status, error_class=res.error_class)
                    break
                res.response.encoding = res.response.encoding or "utf-8"
                html = res.response.text
                cards, meta = parse_topic_page(html, topic, page)
                verdict, signals = detect_block(res.status, html, len(cards))
                snapshot = save_html_snapshot(html, topic, page, ctx)
                ctx.log("html_page_parsed", topic=topic, page=page, url=url, status=res.status,
                        verdict=verdict, signals=signals, snapshot=snapshot, from_cache=res.from_cache, **meta)
                if verdict == "блокировка":
                    stats["html_blocked"] += 1
                    ctx.incident("blocked", f"признаки блокировки на {url}: {'; '.join(signals)}",
                                 source=SOURCE, url=url, snapshot=snapshot)
                    client.breaker(urlparse(url).netloc).trip()   # домен на паузу: не обходим, а останавливаемся
                    ctx.log("breaker_state", level="ERROR", host=urlparse(url).netloc, state="open",
                            reason="детектор блокировки")
                    break
                tstat["strategy"] = meta["strategy"]
                tstat["total"] = meta["topic_total"]
                records = []
                for c in cards:
                    rank += 1
                    records.append({**c, "rank_in_topic": rank})
                html_sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
                prov = {"_url": url, "_status": res.status, "_parser_version": parser_version,
                        "_html_sha256": html_sha, "_snapshot": snapshot, "_strategy": meta["strategy"],
                        "topic": topic, "page": page, "sort_mode": sort_mode}
                write_raw(records, SOURCE, ctx, prov)
                listings.extend({"_run_id": ctx.run_id, "_collected_at": iso(utcnow()), **prov, "payload": r}
                                for r in records)
                stats["html_pages_ok"] += 1
                stats["html_cards"] += len(records)
                tstat["pages"] += 1
                tstat["cards"] += len(records)
                done.setdefault(dkey, {})[str(page)] = ctx.run_id
                if save_checkpoint:
                    save_checkpoint()
                ctx.say(f"  [HTML] {topic} стр. {page}: карточек {len(records)} (стратегия {meta['strategy']})")
                if not cards or (not meta["has_next"] and len(cards) < page_size):
                    break                                   # нет следующей страницы -> нет запроса
    stats["listings"] = listings
    return stats


# ------------------------------------------------------------------ диагностика: нужен ли браузер

def diagnose_rendering(client: PoliteClient, url: str, needle: str | None = None) -> dict:
    """Протокол занятия 4: ответ сервера -> поиск текста -> поиск JSON/XHR -> вывод."""
    res = client.get(url, source="diagnostics", limiter_key="html", conditional=False)
    html = res.response.text if (res.response is not None and res.ok) else ""
    topic = url.split("/topics/")[1].split("?")[0] if "/topics/" in url else ""
    cards, meta = parse_topic_page(html, topic, 1) if html else ([], {"strategy": "none", "cards": 0})
    needle = needle or (cards[0]["full_name"] if cards else topic)
    soup = BeautifulSoup(html, "lxml") if html else None
    json_scripts = len(soup.select("script[type='application/json']")) if soup else 0
    form = soup.select_one("form.ajax-pagination-form[action], form[action*='page=']") if soup else None
    out = {
        "url": url, "status": res.status, "bytes": len(html.encode("utf-8")),
        "cards_found_by_requests": len(cards), "strategy": meta["strategy"],
        "needle": needle, "needle_in_html": bool(needle and needle in html),
        "json_script_blocks": json_scripts,
        "load_more_target": form.get("action") if form else None,
        "найденный_xhr": (f"кнопка Load more — GET {form.get('action')} (обычный HTML, тот же парсер)"
                          if form else None),
    }
    out["вывод"] = ("достаточно HTTP (requests + BeautifulSoup)" if out["cards_found_by_requests"] > 0
                    and out["needle_in_html"] else "нужна ручная проверка: данных в HTML не найдено")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Диагностика HTML-источника: нужен ли браузер")
    ap.add_argument("--diagnose", action="store_true", help="выполнить протокол диагностики")
    ap.add_argument("--topic", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--rate-scale", type=float, default=1.0)
    args = ap.parse_args()
    if not args.diagnose:
        ap.print_help()
        return 0
    cfg = load_config(args.config)
    ctx = RunContext(run_id="diag_" + utcnow().strftime("%Y%m%dT%H%M%SZ"))
    client = PoliteClient(cfg, ctx, rate_scale=args.rate_scale)
    src = cfg["sources"]["html"]
    client.register_limit("html", src["base_url"], src["rate_per_sec"], src["burst"])
    topic = args.topic or src["topics"][0]
    url = topic_url(src["base_url"], topic, 1, src.get("language_filter"))
    robots = client.check_robots(src["base_url"], [url])
    res = diagnose_rendering(client, url)
    res["robots"] = robots.as_dict()
    out = DIRS["reports"] / f"diagnostics_{ctx.run_id}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    (DIRS["reports"] / "diagnostics_latest.json").write_text(json.dumps(res, ensure_ascii=False, indent=2),
                                                             encoding="utf-8")
    for k, v in res.items():
        if k != "robots":
            print(f"{k:28}: {v}")
    print(f"robots.txt: группа {robots.group}, Crawl-delay {robots.crawl_delay}, проверки {robots.checks}")
    print("Сохранено:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
