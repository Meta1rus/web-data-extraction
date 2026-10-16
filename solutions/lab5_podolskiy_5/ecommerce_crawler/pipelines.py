"""Item Pipeline из четырёх звеньев (порядок задан в settings.ITEM_PIPELINES).

100 ValidationPipeline — обязательные поля; иначе DropItem + dropped/missing_fields
200 CleanPipeline      — типизация (цена → Decimal), пробелы, регистр, ISO-даты;
                         некорректная цена → DropItem + dropped/bad_price
300 DedupPipeline      — дубли по бизнес-ключу SKU (не по URL) → dropped/duplicate_sku
400 SQLitePipeline     — пакетная запись (executemany) с UPSERT; соединение
                         открывается в open_spider и закрывается в close_spider

Каждый отброс учитывается в статистике прогона (stats.inc_value), иначе
потеря данных была бы «молчаливой». Методы вызываются без аргумента spider
(актуальный API Scrapy); параметр spider=None оставлен для совместимости со
старыми версиями.
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from itemadapter import ItemAdapter
from scrapy.exceptions import DropItem

CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR", "₽": "RUB"}
MONEY_RE = re.compile(r"(\d{1,3}(?:[ ,]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)")
CENT = Decimal("0.01")
PLACEHOLDER_IMG = re.compile(r"placeholder", re.I)


class StatsMixin:
    """Доступ к статистике и настройкам через crawler (без аргумента spider)."""

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def __init__(self, crawler):
        self.crawler = crawler
        self.settings = crawler.settings

    def count(self, key: str, n: int = 1) -> None:
        self.crawler.stats.inc_value(key, n)


# ----------------------------------------------------------------------------- 100
class ValidationPipeline(StatsMixin):
    """Обязательные поля — список из настройки REQUIRED_FIELDS."""

    def __init__(self, crawler):
        super().__init__(crawler)
        self.required = self.settings.getlist("REQUIRED_FIELDS")
        self.monitored = self.settings.getlist("MONITORED_FIELDS")

    def process_item(self, item, spider=None):
        a = ItemAdapter(item)
        self.count("quality/items_seen")
        # учёт пустых значений по полям варианта (для отчёта о качестве и smoke-check)
        for field in self.monitored:
            value = a.get(field)
            if field == "image_url" and value and PLACEHOLDER_IMG.search(value):
                value = None                    # заглушка вместо фото = изображения нет
            if not value:
                self.count(f"quality/empty/{field}")
        missing = [f for f in self.required if not a.get(f)]
        if missing:
            self.count("dropped/missing_fields")
            for f in missing:
                self.count(f"dropped/missing_fields/{f}")
            raise DropItem(f"нет обязательных полей {missing}: {a.get('product_url')}")
        return item


# ----------------------------------------------------------------------------- 200
def parse_money(text: str | None) -> Decimal | None:
    """'$1,069.50' → Decimal('1069.50'); нечисловой текст → None."""
    if not text:
        return None
    m = MONEY_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", "").replace(" ", "")).quantize(CENT, ROUND_HALF_UP)
    except InvalidOperation:
        return None


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", html.unescape(str(text)).replace("\xa0", " ")).strip()
    return text or None


class CleanPipeline(StatsMixin):
    """Нормализация: деньги → Decimal (точный тип), текст → без лишних пробелов,
    артикул → верхний регистр, валюта → ISO-код, дата → ISO 8601."""

    def process_item(self, item, spider=None):
        a = ItemAdapter(item)
        price_raw = a.get("price_raw") or ""
        amounts = [parse_money(x) for x in re.split(r"\s[–-]\s", price_raw)]
        amounts = [x for x in amounts if x is not None]
        if not amounts or amounts[0] <= 0:
            self.count("dropped/bad_price")
            raise DropItem(f"цена не разбирается: {price_raw!r} ({a.get('product_url')})")
        a["price"] = min(amounts)
        a["price_max"] = max(amounts) if len(amounts) > 1 else None
        a["regular_price"] = parse_money(a.get("regular_price_raw"))
        a["on_sale"] = bool(a["regular_price"] and a["regular_price"] > a["price"])
        symbol = next((s for s in CURRENCY if s in price_raw), None)
        a["currency"] = CURRENCY.get(symbol, "USD") if symbol else "USD"
        if not symbol:
            self.count("clean/currency_assumed")

        a["sku"] = clean_text(a.get("sku")).upper()
        a["name"] = clean_text(a.get("name"))
        if a.get("list_title") and clean_text(a["list_title"]) != a["name"]:
            self.count("clean/title_mismatch_list_vs_card")
        a["categories"] = sorted({c for c in (clean_text(x) for x in a.get("categories") or []) if c},
                                 key=str.lower)
        a["tags"] = sorted({t.lower() for t in (clean_text(x) for x in a.get("tags") or []) if t})
        a["description"] = clean_text(a.get("description"))
        a["short_description"] = clean_text(a.get("short_description"))
        img = a.get("image_url")
        if img and PLACEHOLDER_IMG.search(img):
            self.count("clean/placeholder_image")
            img = None
        a["image_url"] = img
        a["product_id"] = int(a["product_id"]) if str(a.get("product_id") or "").isdigit() else None
        scraped = a.get("scraped_at") or datetime.now(timezone.utc).isoformat()
        a["scraped_at"] = datetime.fromisoformat(scraped).astimezone(timezone.utc).isoformat(timespec="seconds")
        return item


# ----------------------------------------------------------------------------- 300
class DedupPipeline(StatsMixin):
    """Дубли по бизнес-ключу (SKU). Один товар под двумя URL — это один товар."""

    def open_spider(self, spider=None):
        self.seen: set[str] = set()

    def process_item(self, item, spider=None):
        key = ItemAdapter(item)["sku"]
        if key in self.seen:
            self.count("dropped/duplicate_sku")
            raise DropItem(f"дубль SKU {key}")
        self.seen.add(key)
        return item


# ----------------------------------------------------------------------------- 400
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku               TEXT PRIMARY KEY,           -- бизнес-ключ
    name              TEXT NOT NULL,
    price             TEXT NOT NULL,              -- Decimal строкой ('69.00'): без ошибок float
    price_cents       INTEGER NOT NULL,           -- та же цена в центах для арифметики
    price_max         TEXT,
    regular_price     TEXT,
    on_sale           INTEGER NOT NULL DEFAULT 0,
    currency          TEXT NOT NULL,
    categories        TEXT,                       -- JSON-массив
    tags              TEXT,                       -- JSON-массив
    short_description TEXT,
    description       TEXT,
    image_url         TEXT,
    product_url       TEXT NOT NULL,
    product_id        INTEGER,
    sku_source        TEXT,
    list_page_no      INTEGER,
    first_seen_run    TEXT NOT NULL,
    last_seen_run     TEXT NOT NULL,
    scraped_at        TEXT NOT NULL               -- ISO 8601 UTC
);
CREATE TABLE IF NOT EXISTS product_categories (
    sku      TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    category TEXT NOT NULL,
    PRIMARY KEY (sku, category)
);
"""

UPSERT = """
INSERT INTO products (sku, name, price, price_cents, price_max, regular_price, on_sale, currency,
                      categories, tags, short_description, description, image_url, product_url,
                      product_id, sku_source, list_page_no, first_seen_run, last_seen_run, scraped_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(sku) DO UPDATE SET
    name=excluded.name, price=excluded.price, price_cents=excluded.price_cents,
    price_max=excluded.price_max, regular_price=excluded.regular_price, on_sale=excluded.on_sale,
    currency=excluded.currency, categories=excluded.categories, tags=excluded.tags,
    short_description=excluded.short_description, description=excluded.description,
    image_url=excluded.image_url, product_url=excluded.product_url, product_id=excluded.product_id,
    sku_source=excluded.sku_source, list_page_no=excluded.list_page_no,
    last_seen_run=excluded.last_seen_run, scraped_at=excluded.scraped_at
"""


def dec_str(value) -> str | None:
    return None if value is None else str(value)


class SQLitePipeline(StatsMixin):
    """Пакетная запись в SQLite: executemany + UPSERT; соединение живёт весь прогон."""

    def open_spider(self, spider=None):
        path = Path(self.settings.get("SQLITE_PATH", "data/crawl.db"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.batch_size = self.settings.getint("SQLITE_BATCH_SIZE", 50)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.rows: list[tuple] = []
        self.cats: list[tuple] = []

    def process_item(self, item, spider=None):
        a = ItemAdapter(item)
        run_id = a.get("run_id") or "unknown"
        self.rows.append((
            a["sku"], a["name"], dec_str(a["price"]), int(a["price"] * 100), dec_str(a.get("price_max")),
            dec_str(a.get("regular_price")), int(bool(a.get("on_sale"))), a["currency"],
            json.dumps(a.get("categories") or [], ensure_ascii=False),
            json.dumps(a.get("tags") or [], ensure_ascii=False),
            a.get("short_description"), a.get("description"), a.get("image_url"), a["product_url"],
            a.get("product_id"), a.get("sku_source"), a.get("list_page_no"), run_id, run_id,
            a["scraped_at"],
        ))
        self.cats.extend((a["sku"], c) for c in a.get("categories") or [])
        if len(self.rows) >= self.batch_size:
            self.flush()
        return item

    def flush(self) -> None:
        if not self.rows:
            return
        skus = [(r[0],) for r in self.rows]
        with self.conn:                                     # одна транзакция на пакет
            self.conn.executemany(UPSERT, self.rows)
            self.conn.executemany("DELETE FROM product_categories WHERE sku = ?", skus)
            self.conn.executemany("INSERT OR REPLACE INTO product_categories VALUES (?, ?)", self.cats)
        self.count("db/rows_written", len(self.rows))
        self.count("db/batches")
        self.rows.clear()
        self.cats.clear()

    def close_spider(self, spider=None):
        self.flush()
        total = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        self.crawler.stats.set_value("db/rows_total_in_table", total)
        self.conn.close()
