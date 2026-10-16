"""Паук варианта 5: учебный магазин scrapingcourse.com/ecommerce (WooCommerce).

Уровень 1 — страницы каталога (/ecommerce/, /ecommerce/page/N/): карточки товаров
и ссылка «следующая страница». Уровень 2 — карточка товара: название, цена,
артикул (SKU), категории, описание, изображение.

Логика паука — только извлечение. Проверка, очистка, дедупликация и запись в БД
выполняются в pipelines.py.

Запуск (из корня лабораторной):
    scrapy crawl ecommerce                                   # реальный сайт (по умолчанию)
    scrapy crawl ecommerce -a start_url=http://127.0.0.1:18505/ecommerce/ -a run_label=mock
    scrapy crawl ecommerce -a max_pages=3                    # подмножество страниц каталога
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import scrapy
from scrapy.exceptions import IgnoreRequest
from scrapy.spidermiddlewares.httperror import HttpError
from twisted.internet.error import (ConnectionRefusedError, DNSLookupError,
                                    TCPTimedOutError, TimeoutError as TxTimeoutError)

from ecommerce_crawler.items import ProductItem

DEFAULT_START_URL = "https://www.scrapingcourse.com/ecommerce/"

# Селекторы: первый — основной, остальные — фолбэки. Только семантические классы
# WooCommerce, атрибуты data-*, itemprop и rel; индексных XPath нет.
SEL = {
    "card": ["ul.products li.product", "li.type-product", "li[class*='product_cat-']"],
    "card_link": ["a.woocommerce-LoopProduct-link::attr(href)",
                  "a.woocommerce-loop-product__link::attr(href)",
                  "a[href*='/product/']::attr(href)"],
    "card_title": ["h2.woocommerce-loop-product__title::text",
                   ".woocommerce-loop-product__title::text", "h2::text", "h3::text"],
    "card_sku": ["a.add_to_cart_button::attr(data-product_sku)",
                 "[data-product_sku]::attr(data-product_sku)"],
    "card_id": ["a.add_to_cart_button::attr(data-product_id)",
                "[data-product_id]::attr(data-product_id)"],
    "next": ["nav.woocommerce-pagination a.next::attr(href)",
             "a.next.page-numbers::attr(href)", "link[rel='next']::attr(href)"],
    "title": ["h1.product_title::text", "div.summary h1::text", "h1.entry-title::text",
              "[itemprop='name']::text"],
    "price_block": ["div.summary p.price", "p.price", "div.summary span.price",
                    "[itemprop='offers']"],
    "sku": ["div.product_meta span.sku::text", "span.sku::text", "[itemprop='sku']::text"],
    "categories": ["div.product_meta span.posted_in a[rel='tag']::text",
                   "span.posted_in a::text"],
    "tags": ["div.product_meta span.tagged_as a[rel='tag']::text", "span.tagged_as a::text"],
    "short": ["div.woocommerce-product-details__short-description",
              "div.summary [itemprop='description']"],
    "description": ["div#tab-description", "div.woocommerce-Tabs-panel--description"],
    "image": ["div.woocommerce-product-gallery__image a::attr(href)",
              "div.woocommerce-product-gallery img::attr(data-large_image)",
              "div.woocommerce-product-gallery img::attr(src)",
              "img.wp-post-image::attr(src)", "meta[property='og:image']::attr(content)"],
}

WS = re.compile(r"\s+")


def norm(text: str | None) -> str | None:
    """Схлопывает пробелы (включая неразрывные) и обрезает края."""
    if text is None:
        return None
    text = WS.sub(" ", text.replace("\xa0", " ")).strip()
    return text or None


def first(sel, key: str) -> str | None:
    """Первое непустое значение по цепочке селекторов-фолбэков."""
    for css in SEL[key]:
        value = norm(sel.css(css).get())
        if value:
            return value
    return None


def all_values(sel, key: str) -> list[str]:
    for css in SEL[key]:
        values = [v for v in (norm(x) for x in sel.css(css).getall()) if v]
        if values:
            return values
    return []


def block_text(sel, key: str) -> str | None:
    """Текст блока без заголовков h2 (у вкладки «Description» он служебный)."""
    for css in SEL[key]:
        node = sel.css(css)
        if node:
            parts = node.xpath(".//text()[not(ancestor::h2) and not(ancestor::script)]").getall()
            text = norm(" ".join(parts))
            if text:
                return text
    return None


class EcommerceSpider(scrapy.Spider):
    name = "ecommerce"

    def __init__(self, start_url: str | None = None, max_pages: str | None = None,
                 run_label: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_url = start_url or os.environ.get("START_URL") or DEFAULT_START_URL
        self.start_urls = [self.start_url]
        self.allowed_domains = [urlparse(self.start_url).hostname]
        self.max_pages = int(max_pages) if max_pages else None
        self.run_label = run_label or os.environ.get("RUN_LABEL") or ""
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.failed: list[dict] = []

    # --- точки входа ------------------------------------------------------------------
    async def start(self):                     # Scrapy >= 2.13
        for req in self.start_requests():
            yield req

    def start_requests(self):                  # совместимость со Scrapy < 2.13
        for url in self.start_urls:
            yield scrapy.Request(url, callback=self.parse, errback=self.on_error,
                                 cb_kwargs={"page_no": 1}, dont_filter=True)

    # --- уровень 1: страница каталога -------------------------------------------------
    def parse(self, response, page_no: int = 1):
        stats = self.crawler.stats
        stats.inc_value("pages/list")
        cards = []
        for css in SEL["card"]:
            cards = response.css(css)
            if cards:
                break
        if not cards:
            stats.inc_value("pages/list_without_cards")
            self.logger.warning("На странице каталога нет карточек: %s", response.url)
        for position, card in enumerate(cards, start=1):
            href = first(card, "card_link")
            if not href:
                stats.inc_value("cards/without_link")
                continue
            list_ctx = {
                "list_page": response.url,
                "list_page_no": page_no,
                "list_position": position,
                "list_title": first(card, "card_title"),
                "list_price_raw": norm(" ".join(card.css(".price ::text").getall())),
                "list_sku": first(card, "card_sku"),
                "product_id": first(card, "card_id"),
            }
            # priority=10: карточки текущей страницы обрабатываются раньше следующей страницы
            yield response.follow(href, callback=self.parse_item, errback=self.on_error,
                                  cb_kwargs={"list_ctx": list_ctx}, priority=10)

        next_href = first(response, "next")
        if next_href and (self.max_pages is None or page_no < self.max_pages):
            yield response.follow(next_href, callback=self.parse, errback=self.on_error,
                                  cb_kwargs={"page_no": page_no + 1})
        elif next_href:
            stats.inc_value("pages/stopped_by_max_pages")

    # --- уровень 2: карточка товара ---------------------------------------------------
    def parse_item(self, response, list_ctx: dict):
        stats = self.crawler.stats
        ld = self._json_ld_product(response)

        sku, sku_source = first(response, "sku"), "html"
        if not sku or sku.upper() == "N/A":
            sku, sku_source = norm(str(ld.get("sku") or "")) or None, "jsonld"
            if not sku:
                sku, sku_source = list_ctx.get("list_sku"), "list"
        if not sku or sku.upper() == "N/A":
            sku, sku_source = None, None
        if sku_source in ("jsonld", "list"):
            stats.inc_value(f"fields/sku_from_{sku_source}")

        price_raw, regular_raw = self._price(response)
        if not price_raw and ld.get("offers"):
            offers = ld["offers"] if isinstance(ld["offers"], list) else [ld["offers"]]
            price_raw = norm(str(offers[0].get("price") or "")) if offers else None
            if price_raw:
                stats.inc_value("fields/price_from_jsonld")

        image = first(response, "image") or (ld.get("image") if isinstance(ld.get("image"), str) else None)

        item = ProductItem(
            sku=sku,
            name=first(response, "title") or norm(ld.get("name")) or list_ctx.get("list_title"),
            price_raw=price_raw,
            regular_price_raw=regular_raw,
            categories=all_values(response, "categories"),
            tags=all_values(response, "tags"),
            short_description=block_text(response, "short"),
            description=block_text(response, "description") or norm(ld.get("description")),
            image_url=response.urljoin(image) if image else None,
            product_url=response.url,
            product_id=list_ctx.get("product_id"),
            sku_source=sku_source,
            list_page=list_ctx.get("list_page"),
            list_page_no=list_ctx.get("list_page_no"),
            list_position=list_ctx.get("list_position"),
            list_title=list_ctx.get("list_title"),
            list_price_raw=list_ctx.get("list_price_raw"),
            run_id=self.run_id,
            scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        yield item

    @staticmethod
    def _price(response) -> tuple[str | None, str | None]:
        """Текущая цена (с учётом скидки и диапазона) и цена до скидки — как текст."""
        block = None
        for css in SEL["price_block"]:
            found = response.css(css)
            if found:
                block = found[0]
                break
        if block is None:
            return None, None

        def amounts(scope) -> list[str]:
            return [v for v in (norm("".join(a.css("::text").getall()))
                                for a in scope.css(".woocommerce-Price-amount")) if v]

        if block.css("ins"):
            current, regular = amounts(block.css("ins")), amounts(block.css("del"))
        else:
            current, regular = amounts(block), []
        if not current:                         # нестандартный текст («Price on request»)
            raw = norm(" ".join(block.css("::text").getall()))
            return raw, None
        return " – ".join(current), (regular[0] if regular else None)

    def _json_ld_product(self, response) -> dict:
        """Объект Product из JSON-LD (WooCommerce выводит его на карточке) — фолбэк."""
        for raw in response.css("script[type='application/ld+json']::text").getall():
            try:
                data = json.loads(raw)
            except ValueError:
                self.crawler.stats.inc_value("fields/jsonld_invalid")
                continue
            nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
            for node in nodes if isinstance(nodes, list) else []:
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
        return {}

    # --- ошибки конкретного запроса -----------------------------------------------------
    def on_error(self, failure):
        request = failure.request
        if failure.check(HttpError):
            kind = f"http_{failure.value.response.status}"
        elif failure.check(IgnoreRequest):
            kind = "ignored"                    # например, «Forbidden by robots.txt»
        elif failure.check(DNSLookupError):
            kind = "dns"
        elif failure.check(TxTimeoutError, TCPTimedOutError):
            kind = "timeout"
        elif failure.check(ConnectionRefusedError):
            kind = "connection_refused"
        else:
            kind = type(failure.value).__name__
        stats = self.crawler.stats
        stats.inc_value("errback/total")
        stats.inc_value(f"errback/{kind}")
        self.failed.append({"url": request.url, "kind": kind, "error": str(failure.value)[:200]})
        self.logger.warning("Ошибка запроса [%s] %s: %s", kind, request.url, failure.value)

    # --- статистика прогона ---------------------------------------------------------------
    def closed(self, reason):
        stats_dir = Path(self.crawler.settings.get("STATS_DIR", "data"))
        stats_dir.mkdir(parents=True, exist_ok=True)
        numbers = [int(m.group(1)) for p in stats_dir.glob("stats_*.json")
                   if (m := re.match(r"stats_(\d+)", p.name))]
        run_no = max(numbers, default=0) + 1
        suffix = f"_{re.sub(r'[^A-Za-z0-9_-]+', '-', self.run_label)}" if self.run_label else ""
        path = stats_dir / f"stats_{run_no:02d}{suffix}.json"

        raw = self.crawler.stats.get_stats()
        # closed() вызывается раньше, чем CoreStats запишет finish_time/elapsed_time_seconds,
        # поэтому длительность считаем сами от start_time.
        finish = datetime.now(timezone.utc)
        if isinstance(raw.get("start_time"), datetime) and "elapsed_time_seconds" not in raw:
            raw = {**raw, "finish_time": finish,
                   "elapsed_time_seconds": round((finish - raw["start_time"]).total_seconds(), 3)}
        stats = {k: (v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
                 for k, v in raw.items()}
        settings = self.crawler.settings
        stats.update({
            "run_no": run_no,
            "run_label": self.run_label,
            "run_id": self.run_id,
            "close_reason": reason,
            "start_url": self.start_url,
            "max_pages": self.max_pages,
            "settings/CLOSESPIDER_ITEMCOUNT": settings.getint("CLOSESPIDER_ITEMCOUNT"),
            "settings/CONCURRENT_REQUESTS_PER_DOMAIN": settings.getint("CONCURRENT_REQUESTS_PER_DOMAIN"),
            "settings/DOWNLOAD_DELAY": settings.getfloat("DOWNLOAD_DELAY"),
            "settings/AUTOTHROTTLE_ENABLED": settings.getbool("AUTOTHROTTLE_ENABLED"),
            "settings/HTTPCACHE_ENABLED": settings.getbool("HTTPCACHE_ENABLED"),
            "settings/JOBDIR": settings.get("JOBDIR") or "",
            "failed_requests_sample": self.failed[:50],
        })
        path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        self.logger.info("Статистика прогона сохранена: %s", path)
