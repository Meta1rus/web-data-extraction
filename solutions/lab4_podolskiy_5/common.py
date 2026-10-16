"""Общие части ЛР-4 (вариант 5): конфигурация, robots.txt, извлечение и нормализация
записей, общий цикл обхода с предохранителями, журнал, проверка качества, замер памяти.

Используется браузерной реализацией (collect.py), дешёвой альтернативой (light.py),
диагностикой (diagnose.py), замерами (bench.py) и тестом подмены ответов (faults.py).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urljoin, urlsplit

import psutil

# ------------------------------------------------------------------ конфигурация
LAB_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Минимальный разбор .env (KEY=VALUE), без перезаписи уже заданных переменных."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(LAB_DIR / ".env")

BASE_URL = os.getenv("BASE_URL", "https://quotes.toscrape.com").rstrip("/")
USER_AGENT = "PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)"
# Учебные креды: quotes.toscrape.com принимает любой логин/пароль. Это не секрет,
# но по правилам курса значения берутся из окружения (.env), в выводе — маскируются.
USERNAME = os.getenv("QTS_USERNAME", "student")
PASSWORD = os.getenv("QTS_PASSWORD", "student")
POLITE_PAUSE = float(os.getenv("POLITE_PAUSE", "1.0"))      # с между запросами
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))        # с, requests
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "30000"))   # мс, навигация Playwright
ACTION_TIMEOUT_MS = int(os.getenv("ACTION_TIMEOUT_MS", "15000"))
QUOTES_WAIT_MS = int(os.getenv("QUOTES_WAIT_MS", "10000"))   # ожидание карточек на странице
HEADLESS = os.getenv("HEADLESS", "1") != "0"
# Только для тестовой среды: явный путь к Chromium, если версия playwright не совпала
CHROMIUM_PATH = os.getenv("PW_CHROMIUM_PATH") or None

OUT_DIR = Path(os.getenv("OUT_DIR", str(LAB_DIR))).resolve()
ARTIFACTS = OUT_DIR / "artifacts"
METRICS = OUT_DIR / "metrics"
LOGS = OUT_DIR / "logs"
STATE_PATH = OUT_DIR / "auth_state.json"              # storage_state Playwright
LIGHT_STATE_PATH = OUT_DIR / "auth_state_light.json"  # cookies requests.Session
LOG_PATH = OUT_DIR / "collection_log.csv"

for _d in (ARTIFACTS, METRICS, LOGS):
    _d.mkdir(parents=True, exist_ok=True)


def mask(value: str | None, keep: int = 3) -> str:
    if not value:
        return "<пусто>"
    return value[:keep] + "*" * max(len(value) - keep, 3)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ------------------------------------------------------------------ robots.txt
class RobotsGuard:
    """Проверка robots.txt (ROBOTSTXT_OBEY=True). 404 → ограничений нет (как у реального сайта)."""

    def __init__(self, base_url: str, session=None):
        import requests  # локальный импорт: модуль нужен и без requests в части сценариев

        self.base = base_url
        self.status: int | None = None
        self.crawl_delay: float | None = None
        self.parser = urllib.robotparser.RobotFileParser()
        sess = session or requests.Session()
        try:
            r = sess.get(f"{base_url}/robots.txt", timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
            self.status = r.status_code
            if r.status_code == 200:
                self.parser.parse(r.text.splitlines())
                self.crawl_delay = self.parser.crawl_delay(USER_AGENT)
            elif r.status_code in (401, 403):
                self.parser.disallow_all = True
            else:                       # 404 и прочее — robots.txt нет, разрешено всё
                self.parser.allow_all = True
        except requests.RequestException as e:
            # robots.txt недоступен — консервативно продолжаем как «разрешено», но фиксируем
            self.status = -1
            self.parser.allow_all = True
            print(f"[robots] не удалось получить robots.txt: {type(e).__name__}")

    def allowed(self, url: str) -> bool:
        return self.parser.can_fetch(USER_AGENT, url)

    def pause(self, base_pause: float) -> float:
        return max(base_pause, self.crawl_delay or 0.0)


# ------------------------------------------------------------------ извлечение
# Карточка цитаты: микроразметка schema.org (устойчивее класса), фолбэк — класс компонента.
QUOTE_SEL = '[itemtype*="schema.org/CreativeWork"], div.quote'

# Массовое извлечение одним вызовом в контексте страницы (eval_on_selector_all).
# textContent, а не innerText: не зависит от CSS (блокируем стили — текст тот же).
JS_EXTRACT = r"""
els => els.map(el => {
  const pick = (...sels) => { for (const s of sels) { const n = el.querySelector(s); if (n) return n; } return null; };
  const txt = n => n ? n.textContent.replace(/\s+/g, ' ').trim() : null;
  const links = Array.from(el.querySelectorAll('a[href]'));
  const about = links.find(a => a.getAttribute('href').includes('/author/') && !/goodreads/i.test(a.getAttribute('href'))) || null;
  const gr = links.find(a => /goodreads\.com/i.test(a.getAttribute('href'))) || links.find(a => /goodreads/i.test(a.textContent)) || null;
  const kw = pick('meta[itemprop="keywords"]', 'meta.keywords');
  return {
    text: txt(pick('[itemprop="text"]', '.text')),
    author: txt(pick('[itemprop="author"]', '.author')),
    author_href: about ? about.getAttribute('href') : null,
    goodreads_href: gr ? gr.getAttribute('href') : null,
    keywords: kw ? kw.getAttribute('content') : null,
    tag_links: Array.from(el.querySelectorAll('.tags a[href*="/tag/"], a.tag')).map(a => ({name: txt(a), href: a.getAttribute('href')})),
  };
})
"""


def extract_quotes_bs(soup) -> list[dict]:
    """Та же логика, что JS_EXTRACT, но для BeautifulSoup (дешёвая альтернатива)."""

    def txt(node):
        return re.sub(r"\s+", " ", node.get_text()).strip() if node else None

    out = []
    for el in soup.select(QUOTE_SEL):
        def pick(*sels):
            for s in sels:
                n = el.select_one(s)
                if n is not None:
                    return n
            return None

        links = el.select("a[href]")
        about = next((a for a in links if "/author/" in a["href"]
                      and not re.search("goodreads", a["href"], re.I)), None)
        gr = (next((a for a in links if re.search(r"goodreads\.com", a["href"], re.I)), None)
              or next((a for a in links if re.search("goodreads", a.get_text(), re.I)), None))
        kw = pick('meta[itemprop="keywords"]', "meta.keywords")
        tag_nodes = el.select('.tags a[href*="/tag/"], a.tag')
        out.append({
            "text": txt(pick('[itemprop="text"]', ".text")),
            "author": txt(pick('[itemprop="author"]', ".author")),
            "author_href": about["href"] if about else None,
            "goodreads_href": gr["href"] if gr else None,
            "keywords": kw.get("content") if kw else None,
            "tag_links": [{"name": txt(a), "href": a.get("href")} for a in tag_nodes],
        })
    return out


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip("“”\"").strip()


def quote_id(text: str, author: str) -> str:
    """Бизнес-ключ цитаты: у цитат на сайте нет id/URL, поэтому ключ — хеш
    нормализованных (NFKC, схлопнутые пробелы, без декоративных кавычек) текста и автора."""
    basis = f"{norm_text(text).casefold()}|{norm_text(author).casefold()}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def pair_key(tag: str, qid: str) -> str:
    """Бизнес-ключ записи итогового набора «цитата в разделе тега»."""
    return hashlib.sha1(f"{tag.casefold()}|{qid}".encode("utf-8")).hexdigest()[:16]


def normalize_quote(raw: dict, page_url: str, page_no: int, auth_ok: bool,
                    approach: str) -> dict:
    text = norm_text(raw.get("text"))
    author = norm_text(raw.get("author"))
    tags = [t.strip() for t in (raw.get("keywords") or "").split(",") if t.strip()]
    if not tags:  # фолбэк: метатег отсутствует → тексты ссылок-тегов
        tags = [norm_text(t.get("name")) for t in raw.get("tag_links") or [] if t.get("name")]
    author_href = raw.get("author_href") or ""
    m = re.search(r"/author/([^/?#]+)", author_href)
    gr = raw.get("goodreads_href") or ""
    return {
        "quote_id": quote_id(text, author),
        "text": text,
        "author": author,
        "author_slug": m.group(1) if m else "",
        "author_url": urljoin(page_url, author_href) if author_href else "",
        "goodreads_url": urljoin(page_url, gr) if gr else "",
        "tags_all": ",".join(tags),
        "n_tags": len(tags),
        "page_no": page_no,
        "page_url": page_url,
        "auth_ok": auth_ok,
        "collected_at": now_iso(),
        "approach": approach,
    }


def tag_links_of(raws: list[dict], page_url: str) -> dict[str, str]:
    """Тег → абсолютный URL его первой страницы (из href ссылок карточек)."""
    out: dict[str, str] = {}
    for r in raws:
        for t in r.get("tag_links") or []:
            name = norm_text(t.get("name"))
            if name and t.get("href") and name not in out:
                out[name] = urljoin(page_url, t["href"])
    return out


def page_no_of(url: str) -> int:
    m = re.search(r"/page/(\d+)", urlsplit(url).path)
    return int(m.group(1)) if m else 1


# ------------------------------------------------------------------ журнал
LOG_FIELDS = ["run_id", "approach", "stage", "section", "iteration", "page_no", "url",
              "n_elements", "new_records", "total_records", "duration_s", "requests",
              "bytes", "status", "note", "ts"]


class CollectionLog:
    """collection_log.csv: строки текущего запуска заменяют строки того же подхода
    (approach) из прошлых запусков; строки других подходов сохраняются."""

    def __init__(self, approach: str, rid: str, path: Path = LOG_PATH):
        self.approach, self.rid, self.path = approach, rid, path
        self.rows: list[dict] = []

    def add(self, **kw) -> dict:
        row = {k: "" for k in LOG_FIELDS}
        row.update(run_id=self.rid, approach=self.approach, ts=now_iso())
        row.update({k: v for k, v in kw.items() if k in LOG_FIELDS})
        self.rows.append(row)
        return row

    def flush(self) -> None:
        keep: list[dict] = []
        if self.path.exists():
            with self.path.open(encoding="utf-8", newline="") as f:
                keep = [r for r in csv.DictReader(f) if r.get("approach") != self.approach]
        with self.path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            w.writeheader()
            w.writerows(keep + self.rows)


# ------------------------------------------------------------------ память
class PeakRSS:
    """Фоновый замер пиковой памяти: RSS процесса Python + всех дочерних процессов
    (драйвер Playwright и процессы Chromium). Сумма RSS — верхняя оценка (общие
    страницы памяти считаются несколько раз)."""

    def __init__(self, interval: float = 0.1):
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self.proc = psutil.Process()

    def _sample(self) -> int:
        total = 0
        procs = [self.proc]
        try:
            procs += self.proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass  # список детей недоступен на этом шаге — считаем только сам процесс
        for p in procs:
            try:
                total += p.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue  # процесс завершился между перечислением и замером
        return total

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, self._sample())
            self._stop.wait(self.interval)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=2)
        self.peak = max(self.peak, self._sample())

    @property
    def peak_mb(self) -> float:
        return round(self.peak / 2**20, 1)


# ------------------------------------------------------------------ общий цикл обхода
@dataclass
class PageResult:
    url: str
    page_no: int
    raws: list[dict]
    next_url: str | None
    auth_ok: bool
    status: int
    requests: int = 0
    bytes: int = 0


class PageError(Exception):
    """Сбой на странице (таймаут ожидания, HTTP-ошибка, сеть). Артефакты уже сохранены бэкендом."""

    def __init__(self, kind: str, url: str, detail: str = ""):
        super().__init__(f"{kind}: {url} {detail}".strip())
        self.kind, self.url, self.detail = kind, url, detail


class Backend(Protocol):
    approach: str

    def load(self, url: str) -> PageResult: ...
    def follow_next(self, cur: PageResult) -> PageResult | None: ...
    def reauth(self) -> bool: ...


@dataclass
class Limits:
    max_iterations: int = int(os.getenv("MAX_ITERATIONS", "600"))      # циклов на весь запуск
    max_pages_per_section: int = int(os.getenv("MAX_PAGES_PER_SECTION", "30"))
    stall_limit: int = int(os.getenv("STALL_LIMIT", "2"))             # страниц без прироста подряд
    max_records: int = int(os.getenv("MAX_RECORDS", "5000"))
    max_failures: int = int(os.getenv("MAX_FAILURES", "10"))
    max_reauth: int = 1
    max_runtime_s: float = float(os.getenv("MAX_RUNTIME_S", "1800"))


class AuthLost(Exception):
    pass


class TooManyFailures(Exception):
    pass


@dataclass
class Crawler:
    """Обход разделов с тремя (и более) предохранителями — одинаковый для браузера и requests:
    1) максимум итераций (на запуск и на раздел); 2) остановка при отсутствии прироста
    `stall_limit` страниц подряд; 3) лимит записей; плюс лимит сбоев и времени."""

    backend: Backend
    log: CollectionLog
    robots: RobotsGuard
    limits: Limits = field(default_factory=Limits)
    pause: float = POLITE_PAUSE
    require_auth: bool = True
    quotes: dict = field(default_factory=dict)     # quote_id → запись ленты
    pairs: dict = field(default_factory=dict)      # pair_key → запись «цитата × тег»
    tag_urls: dict = field(default_factory=dict)   # тег → URL первой страницы
    iteration: int = 0
    failures: int = 0
    reauths: int = 0
    raw_rows: int = 0
    stop_reasons: dict = field(default_factory=dict)
    active_s: float = 0.0
    paused_s: float = 0.0
    t_start: float = field(default_factory=time.perf_counter)

    # --- служебное
    def _sleep(self):
        p = self.robots.pause(self.pause)
        if p > 0:
            time.sleep(p)       # пауза вежливости между запросами, НЕ ожидание данных
            self.paused_s += p

    def _budget_left(self) -> str | None:
        if self.iteration >= self.limits.max_iterations:
            return "max_iterations"
        records = len(self.pairs) if self.pairs else len(self.quotes)  # итоговый набор — пары
        if records >= self.limits.max_records:
            return "max_records"
        if time.perf_counter() - self.t_start > self.limits.max_runtime_s:
            return "max_runtime"
        return None

    def _ingest(self, stage: str, section: str, res: PageResult) -> int:
        before = len(self.pairs) if stage == "tag" else len(self.quotes)
        for raw in res.raws:
            rec = normalize_quote(raw, res.url, res.page_no, res.auth_ok, self.backend.approach)
            self.raw_rows += 1
            self.quotes.setdefault(rec["quote_id"], rec)
            if stage == "tag":
                key = pair_key(section, rec["quote_id"])
                if key not in self.pairs:
                    self.pairs[key] = {"record_key": key, "tag": section, **rec}
        self.tag_urls.update({k: v for k, v in tag_links_of(res.raws, res.url).items()
                              if k not in self.tag_urls})
        after = len(self.pairs) if stage == "tag" else len(self.quotes)
        return after - before

    def _fetch(self, fn: Callable[[], PageResult | None], url: str) -> PageResult | None:
        """Загрузка с проверкой авторизации и одной повторной попыткой при сбое."""
        last: PageError | None = None
        for attempt in (1, 2):
            try:
                res = fn() if attempt == 1 else self.backend.load(url)
            except PageError as e:
                last = e
                self.failures += 1
                if self.failures >= self.limits.max_failures:
                    raise TooManyFailures(str(e)) from e
                if attempt == 1:
                    self._sleep()
                continue
            if res is None:
                return None
            if self.require_auth and not res.auth_ok:
                if self.reauths >= self.limits.max_reauth or not self.backend.reauth():
                    raise AuthLost(f"сессия потеряна на {res.url}")
                self.reauths += 1
                res = self.backend.load(res.url)
                if not res.auth_ok:
                    raise AuthLost(f"повторный вход не восстановил сессию ({res.url})")
            return res
        assert last is not None
        raise last

    # --- обход одного раздела (лента или тег) с пагинацией
    def crawl_section(self, stage: str, section: str, start_url: str) -> str:
        stalls = 0
        res: PageResult | None = None
        for page_idx in range(1, self.limits.max_pages_per_section + 1):
            reason = self._budget_left()
            if reason:
                return self._stop(section, reason)
            target = start_url if res is None else (res.next_url or "")
            if not self.robots.allowed(target):
                return self._stop(section, "robots_disallow")
            if self.iteration > 0:
                self._sleep()
            self.iteration += 1
            t0 = time.perf_counter()
            try:
                if res is None:
                    res = self._fetch(lambda: self.backend.load(start_url), start_url)
                else:
                    prev = res
                    res = self._fetch(lambda: self.backend.follow_next(prev), target)
            except PageError as e:
                dt = time.perf_counter() - t0
                self.active_s += dt
                self.log.add(stage=stage, section=section, iteration=self.iteration,
                             page_no=page_no_of(target), url=e.url, n_elements=0,
                             new_records=0, total_records=self._total(stage),
                             duration_s=round(dt, 3), status=e.kind, note=e.detail[:200])
                return self._stop(section, f"error:{e.kind}")
            dt = time.perf_counter() - t0
            self.active_s += dt
            if res is None:
                return self._stop(section, "no_next")
            new = self._ingest(stage, section, res)
            self.log.add(stage=stage, section=section, iteration=self.iteration,
                         page_no=res.page_no, url=res.url, n_elements=len(res.raws),
                         new_records=new, total_records=self._total(stage),
                         duration_s=round(dt, 3), requests=res.requests, bytes=res.bytes,
                         status="ok" if res.raws else "empty",
                         note="" if res.auth_ok else "no_auth")
            print(f"[{self.backend.approach}] #{self.iteration:>3} {stage}:{section:<22} "
                  f"стр.{res.page_no:<2} элементов={len(res.raws):<2} прирост={new:<2} "
                  f"всего={self._total(stage):<4} {dt:5.2f} c", flush=True)
            stalls = stalls + 1 if new == 0 else 0
            if stalls >= self.limits.stall_limit:
                return self._stop(section, "stall")
            if not res.next_url:
                return self._stop(section, "no_next")
        return self._stop(section, "max_pages_per_section")

    def _total(self, stage: str) -> int:
        return len(self.pairs) if stage == "tag" else len(self.quotes)

    def _stop(self, section: str, reason: str) -> str:
        self.stop_reasons[section] = reason
        return reason

    # --- полный сценарий варианта 5
    def run(self, max_tags: int | None = None, after_feed: Callable[[], None] | None = None) -> None:
        self.crawl_section("feed", "feed", f"{BASE_URL}/page/1/")
        if after_feed is not None:
            after_feed()
        tags = sorted(self.tag_urls)
        if max_tags is not None:
            tags = tags[:max_tags]
        print(f"[{self.backend.approach}] тегов для обхода: {len(tags)}", flush=True)
        for tag in tags:
            if self._budget_left():
                self.stop_reasons["__run__"] = self._budget_left()
                break
            self.crawl_section("tag", tag, self.tag_urls[tag])


# ------------------------------------------------------------------ выгрузка и качество
PAIR_COLUMNS = ["record_key", "quote_id", "tag", "text", "author", "author_slug", "author_url",
                "goodreads_url", "tags_all", "n_tags", "page_no", "page_url", "auth_ok",
                "collected_at", "approach"]
QUOTE_COLUMNS = [c for c in PAIR_COLUMNS if c not in ("record_key", "tag")]
REQUIRED_FIELDS = ["text", "author", "author_url", "goodreads_url", "tag"]


def to_frames(crawler: Crawler):
    import pandas as pd

    pairs = pd.DataFrame(list(crawler.pairs.values()), columns=PAIR_COLUMNS)
    quotes = pd.DataFrame(list(crawler.quotes.values()), columns=QUOTE_COLUMNS)
    for df in (pairs, quotes):
        df["n_tags"] = pd.to_numeric(df["n_tags"]).astype("Int64")
        df["page_no"] = pd.to_numeric(df["page_no"]).astype("Int64")
        df["auth_ok"] = df["auth_ok"].astype("boolean")
    if not pairs.empty:
        pairs = pairs.sort_values(["tag", "page_no", "quote_id"]).reset_index(drop=True)
    return pairs, quotes


def typed(df):
    """Типизация после чтения CSV (для анализа и сверки)."""
    import pandas as pd

    df = df.copy()
    for c in ("n_tags", "page_no"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    if "auth_ok" in df:
        df["auth_ok"] = df["auth_ok"].map({True: True, False: False, "True": True,
                                           "False": False}).astype("boolean")
    if "collected_at" in df:
        df["collected_at"] = pd.to_datetime(df["collected_at"], utc=True, errors="coerce")
    for c in df.columns:   # pandas 2.x — object, pandas 3.x — str
        if pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c]):
            if c in ("auth_ok", "collected_at"):
                continue
            df[c] = df[c].fillna("").astype(str).str.strip()
    return df


def quality_report(df, key: str = "record_key", required=None) -> dict:
    required = [c for c in (required or REQUIRED_FIELDS) if c in df.columns]
    empty = {c: round(float((df[c].isna() | (df[c].astype(str).str.strip() == "")).mean()), 4)
             if len(df) else None for c in required}
    return {
        "records": int(len(df)),
        "unique_quotes": int(df["quote_id"].nunique()) if "quote_id" in df else None,
        "unique_tags": int(df["tag"].nunique()) if "tag" in df else None,
        "unique_authors": int(df["author"].nunique()) if "author" in df else None,
        "duplicates_by_key": int(df.duplicated(subset=[key]).sum()) if key in df else None,
        "empty_share": empty,
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
    }


def smoke_check(df, required=None, threshold: float = 0.05, name: str = "набор") -> list[str]:
    """Smoke-check после сбора: наличие ключевых полей и доля пустых значений ≤ порога.
    Печатает предупреждения; возвращает их список (пустой — проверка пройдена)."""
    required = required or REQUIRED_FIELDS
    warnings: list[str] = []
    if len(df) == 0:
        warnings.append(f"{name}: 0 записей")
    for c in required:
        if c not in df.columns:
            warnings.append(f"{name}: нет столбца «{c}» — вероятно, сменилась разметка")
            continue
        share = float((df[c].isna() | (df[c].astype(str).str.strip() == "")).mean()) if len(df) else 1.0
        if share > threshold:
            hint = " (не видно «(Goodreads page)» — сессия не активна?)" if c == "goodreads_url" else ""
            warnings.append(f"{name}: доля пустых «{c}» = {share:.1%} > {threshold:.0%}{hint}")
    for w in warnings:
        print("ПРЕДУПРЕЖДЕНИЕ smoke-check:", w)
    if not warnings:
        print(f"smoke-check {name}: OK (ключевые поля заполнены, порог пустых {threshold:.0%})")
    return warnings
