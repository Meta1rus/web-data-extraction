"""Разбор HTML-страниц GitHub и общие функции нормализации значений.

На этапе сбора парсер извлекает ТОЛЬКО сырые строки «как на странице»
(`*_raw`): «54.3k», «53,912», «Python 99.8%». Превращение их в числа делает
очистка (шаг 3, типизация) функциями `parse_compact_number` / `parse_percent`.
Поэтому при исправлении правил разбора чистый слой пересобирается из raw без
повторного обращения к сайту.

⚠ Селекторы составлены по разметке github.com, известной на 2025–2026 гг.,
и проверены на HTML-фикстурах (tests/fixtures/html). Первый реальный прогон
должен их подтвердить (см. README, раздел «Риски»). Для каждого поля есть
запасной путь (fallback), а отсутствие поля даёт NULL, а не падение.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------- текст и URL
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)


def normalize_text(value):
    """NFKC, неразрывные и zero-width символы, схлопывание пробелов, обрезка."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    v = unicodedata.normalize("NFKC", value).replace("\xa0", " ").translate(ZERO_WIDTH)
    v = " ".join(v.split())
    return v or None


TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|ref|ref_cta|ref_loc|ref_page|source|fbclid|gclid|yclid|mc_[a-z]+)$", re.I)
SLUG_RX = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38})/[a-z0-9._-]{1,100}$")


def canonical_url(url: str | None) -> str | None:
    """Убирает параметры трекинга, фрагмент и завершающий слэш; хост — в нижний регистр."""
    url = normalize_text(url)
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def repo_slug(value: str | None) -> str | None:
    """Бизнес-ключ репозитория «owner/name» в нижнем регистре.

    Принимает full_name из API, адрес страницы github.com/{o}/{r}/…, адрес API
    api.github.com/repos/{o}/{r}. Параметры трекинга и регистр не влияют на ключ.
    """
    value = normalize_text(value)
    if not value:
        return None
    if "://" in value:
        parts = [p for p in urlsplit(value).path.split("/") if p]
        if parts and parts[0].lower() == "repos":    # адрес API
            parts = parts[1:]
        if len(parts) < 2:
            return None
        owner, name = parts[0], parts[1]
    else:
        bits = value.strip("/").split("/")
        if len(bits) != 2:
            return None
        owner, name = bits
    name = re.sub(r"\.git$", "", name)
    slug = f"{owner}/{name}".lower()
    return slug if SLUG_RX.match(slug) else None


def issue_number_from_url(url: str | None) -> int | None:
    m = re.search(r"/(?:issues|pull)/(\d+)(?:[/?#]|$)", url or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- числа со страницы
_COMPACT = re.compile(r"^(\d+(?:\.\d+)?)([km]?)(\+?)$")


def parse_compact_number(raw) -> tuple[int | None, bool]:
    """«54.3k» → 54300, «53,912» → 53912, «1.2m» → 1200000, «5,000+» → (5000, True).

    Возвращает (число, это_нижняя_граница). Нераспознанное → (None, False):
    пропуск остаётся пропуском, никаких подстановок.
    """
    if raw is None:
        return None, False
    t = normalize_text(str(raw))
    if not t:
        return None, False
    t = t.lower().replace(",", "").replace(" ", "")
    m = _COMPACT.match(t)
    if not m:
        return None, False
    try:
        num = Decimal(m.group(1))
    except InvalidOperation:
        return None, False
    num *= {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    return int(num), bool(m.group(3))


def parse_percent(raw) -> float | None:
    t = normalize_text(str(raw)) if raw is not None else None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?$", t or "")
    return float(m.group(1)) if m else None


def split_language(raw: str | None) -> tuple[str | None, float | None]:
    """«Python 99.8%» → ("Python", 99.8); «Jupyter Notebook 0.1%» → ("Jupyter Notebook", 0.1)."""
    t = normalize_text(raw)
    m = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?)\s*%?$", t or "")
    if not m:
        return None, None
    return m.group(1), float(m.group(2))


# ---------------------------------------------------------------- разбор HTML
def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _text(node: Tag | None) -> str | None:
    return normalize_text(node.get_text(" ", strip=True)) if node else None


def _meta(soup: BeautifulSoup, name: str) -> str | None:
    node = soup.select_one(f'meta[name="{name}"]') or soup.select_one(f'meta[property="{name}"]')
    return normalize_text(node.get("content")) if node else None


def _section(soup: BeautifulSoup, title: str) -> tuple[Tag | None, Tag | None]:
    """Находит в боковой колонке заголовок h2 «About»/«Releases»/… и его ячейку."""
    for h2 in soup.find_all("h2"):
        text = _text(h2) or ""
        if text == title or text.startswith(title + " "):
            cell = h2.find_parent(class_="BorderGrid-cell") or h2.parent
            return h2, cell
    return None, None


def _counter(h2: Tag | None) -> tuple[str | None, str | None]:
    """Значение счётчика рядом с заголовком: (текст «53.9k», title «53,912»)."""
    if h2 is None:
        return None, None
    node = h2.select_one(".Counter")
    if node is None:
        return None, None
    return _text(node), normalize_text(node.get("title"))


def _embedded_about(soup: BeautifulSoup) -> dict:
    """Данные боковой панели из встроенного JSON React-приложения GitHub.

    С 2025–2026 гг. страница репозитория рендерится React: в исходном HTML боковая
    панель — «скелет» (SkeletonText), а значения лежат в
    <script type="application/json" data-target="react-app.embeddedData">
    по пути payload.sidebarAbout (или payload.codeViewRepoRoute.overview.sidebarAboutData).
    Это вариант «данные в разметке» из занятия 4: браузер не нужен.
    """
    import json as _json
    for node in soup.select('script[type="application/json"][data-target$="embeddedData"]'):
        try:
            data = _json.loads(node.string or "")
        except ValueError:
            continue  # не JSON — смотрим следующий блок
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            continue
        about = payload.get("sidebarAbout")
        if not isinstance(about, dict):
            about = (((payload.get("codeViewRepoRoute") or {}).get("overview") or {})
                     .get("sidebarAboutData"))
        if isinstance(about, dict) and "stargazerCount" in about:
            return about
    return {}


def parse_repo_page(html: str) -> dict:
    """Страница репозитория github.com/{owner}/{repo} → словарь сырых значений."""
    soup = _soup(html)
    out: dict = {}

    out["repo_nwo_raw"] = _meta(soup, "octolytics-dimension-repository_nwo")
    out["repository_id_raw"] = _meta(soup, "octolytics-dimension-repository_id")

    # --- About: описание, домашняя страница, темы, лицензия
    about_h2, about = _section(soup, "About")
    desc = about.select_one("p.f4") if about else None
    out["about_raw"] = _text(desc)
    out["about_from"] = "sidebar" if out["about_raw"] else None
    if not out["about_raw"]:
        meta_desc = _meta(soup, "description")
        nwo = out["repo_nwo_raw"] or ""
        if meta_desc and not meta_desc.startswith("Contribute to "):
            out["about_raw"] = re.sub(rf"\s+-\s+{re.escape(nwo)}$", "", meta_desc) if nwo else meta_desc
            out["about_from"] = "meta"

    home = None
    if about:
        home = about.select_one('a[role="link"][href^="http"]') or next(
            (a for a in about.select('span.css-truncate a[href^="http"]')
             if "github.com" not in a.get("href", "")), None)
    out["homepage_raw"] = normalize_text(home.get("href")) if home else None

    scope = about or soup
    out["topics_raw"] = [t for t in (_text(a) for a in scope.select("a.topic-tag")) if t]

    lic = None
    if about:
        for a in about.select("a"):
            if a.select_one("svg.octicon-law") or re.search(r"licen[cs]e$", _text(a) or "", re.I):
                lic = a
                break
    out["license_raw"] = _text(lic)

    # --- Счётчики: звёзды, форки, наблюдатели (в шапке — точное число в title)
    star = soup.select_one("#repo-stars-counter-star")
    out["stars_raw"] = _text(star)
    out["stars_title_raw"] = normalize_text(star.get("title")) if star else None
    if not out["stars_raw"] and about:
        out["stars_raw"] = _text(about.select_one('a[href$="/stargazers"] strong'))

    fork = soup.select_one("#repo-network-counter")
    out["forks_raw"] = _text(fork)
    out["forks_title_raw"] = normalize_text(fork.get("title")) if fork else None
    if not out["forks_raw"] and about:
        out["forks_raw"] = _text(about.select_one('a[href$="/forks"] strong'))

    out["watchers_raw"] = _text(about.select_one('a[href$="/watchers"] strong')) if about else None

    # --- Releases: число релизов и последний релиз
    rel_h2, rel = _section(soup, "Releases")
    out["releases_count_raw"], out["releases_count_title_raw"] = _counter(rel_h2)
    latest = rel.select_one('a[href*="/releases/tag/"]') if rel else None
    if latest is not None:
        tag_node = latest.select_one(".css-truncate-target")
        out["latest_release_tag_raw"] = _text(tag_node) or unquote(latest["href"].split("/releases/tag/", 1)[1])
        when = latest.select_one("relative-time[datetime], local-time[datetime]")
        out["latest_release_date_raw"] = normalize_text(when.get("datetime")) if when else None
    else:
        out["latest_release_tag_raw"] = None
        out["latest_release_date_raw"] = None

    # --- Used by и Contributors: только число со страницы (сами списки robots.txt запрещает)
    used_h2, _ = _section(soup, "Used by")
    out["used_by_raw"], out["used_by_title_raw"] = _counter(used_h2)
    contrib_h2, _ = _section(soup, "Contributors")
    out["contributors_raw"], out["contributors_title_raw"] = _counter(contrib_h2)

    # --- Languages: «Python 99.8%»
    langs: list[str] = []
    lang_h2, lang_cell = _section(soup, "Languages")
    if lang_cell is not None:
        for li in lang_cell.select("li"):
            name = li.select_one("span.text-bold, span.color-fg-default")
            pct = name.find_next_sibling("span") if name else None
            if name and pct:
                langs.append(f"{_text(name)} {_text(pct)}")
        if not langs:
            for item in lang_cell.select("span.Progress-item[aria-label]"):
                langs.append(normalize_text(item["aria-label"]) + "%")
    out["languages_raw"] = langs

    # --- Основной путь для React-версии страницы: встроенный JSON (значения «как на странице»)
    emb = _embedded_about(soup)
    if emb:
        if emb.get("description"):
            out["about_raw"] = normalize_text(emb["description"])
            out["about_from"] = "embedded_json"
        if emb.get("website") and not out.get("homepage_raw"):
            out["homepage_raw"] = normalize_text(emb["website"])
        if emb.get("topics") and not out.get("topics_raw"):
            out["topics_raw"] = [normalize_text(t.get("name")) for t in emb["topics"]
                                 if isinstance(t, dict) and t.get("name")]
        lic = ((emb.get("repo") or {}).get("license") or {})
        if lic and not out.get("license_raw"):
            out["license_raw"] = normalize_text(lic.get("name") or lic.get("spdxId"))
        for key, field in (("stargazerCount", "stars"), ("forksCount", "forks"), ("watcherCount", "watchers")):
            if emb.get(key) is not None:
                out[f"{field}_raw"] = str(emb[key])          # точное число из JSON
                if f"{field}_title_raw" in out:
                    out[f"{field}_title_raw"] = str(emb[key])
    # Языки, «Used by», число контрибьюторов и релизов в исходном HTML не приходят:
    # React-страница подгружает их отдельными запросами после загрузки (в HTML — SkeletonText).
    return out


def _release_box(link: Tag) -> Tag:
    """Ближайший предок ссылки, в котором есть дата и нет чужих релизов."""
    node = link.parent
    while node is not None and node.name not in ("body", "html", "[document]"):
        if node.select_one("relative-time[datetime], local-time[datetime]"):
            titles = {a.get("href") for a in node.select('a[href*="/releases/tag/"]') if _text(a)}
            return node if len(titles) <= 1 else (link.find_parent(class_="Box") or link.parent)
        node = node.parent
    return link.find_parent(class_="Box") or link.parent


def parse_releases_page(html: str) -> tuple[list[dict], str | None]:
    """Страница github.com/{o}/{r}/releases → (список релизов, ссылка на след. страницу)."""
    soup = _soup(html)
    releases: list[dict] = []
    seen: set[str] = set()

    def from_container(box: Tag, link: Tag) -> dict:
        href = link.get("href", "")
        when = box.select_one("relative-time[datetime], local-time[datetime]")
        commit = box.select_one('a[href*="/commit/"]')
        sha = _text(commit.select_one("code")) if commit else None
        if commit is not None and not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
            m = re.search(r"/commit/([0-9a-fA-F]{7,40})", commit.get("href", ""))
            sha = m.group(1)[:7] if m else None          # запасной путь — из адреса ссылки на коммит
        return {
            "tag_href_raw": href,
            "tag_raw": unquote(href.split("/releases/tag/", 1)[1]) if "/releases/tag/" in href else None,
            "title_raw": _text(link),
            "datetime_raw": normalize_text(when.get("datetime")) if when else None,
            "labels_raw": [t for t in (_text(s) for s in box.select("span.Label")) if t],
            "commit_sha_raw": sha,
        }

    for section in soup.select("section"):
        links = [a for a in section.select('a[href*="/releases/tag/"]') if _text(a)]
        if not links:
            continue
        rec = from_container(section, links[0])
        if rec["tag_href_raw"] in seen:
            continue
        seen.add(rec["tag_href_raw"])
        releases.append(rec)

    if not releases:   # запасной путь: разметка без <section>
        for a in soup.select('a.Link--primary[href*="/releases/tag/"], h2 a[href*="/releases/tag/"]'):
            href = a.get("href", "")
            if href in seen or not _text(a):
                continue
            seen.add(href)
            releases.append(from_container(_release_box(a), a))

    for pos, rec in enumerate(releases, start=1):
        rec["position_raw"] = pos
    nxt = soup.select_one("a.next_page[href], a[rel='next'][href]")
    return releases, (nxt["href"] if nxt else None)
