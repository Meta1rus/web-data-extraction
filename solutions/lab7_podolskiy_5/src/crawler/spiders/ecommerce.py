"""Паук лабораторной № 5 (каталог WooCommerce → карточка), доработанный для лабораторной № 7.

Что добавлено к исходному сборщику:
* page_kind в meta — детектор знает, какие элементы ожидать на странице;
* обработка 304 (handle_httpstatus_list): страница каталога не изменилась → ссылки берутся
  из «водяного знака» (state/watermark.json), карточка не изменилась → запись в БД актуальна;
* инкрементальность (режим C): карточка не запрашивается, если её «отпечаток» в списке
  (название + цена + картинка) не изменился и проверка была недавно (revalidate_after_s);
* второй домен (extra_url) — для демонстрации изоляции отказа предохранителем;
* resume_deferred — дозабор URL, отложенных прошлым прогоном (state/deferred.json).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import scrapy
from scrapy.exceptions import IgnoreRequest
from scrapy.spidermiddlewares.httperror import HttpError

from src.crawler.items import ProductItem
from src.crawler.runtime import get_runtime, host_of

DEFAULT_START_URL = "https://www.scrapingcourse.com/ecommerce/"

SEL = {
    "card": ["ul.products li.product", "li.type-product", "li[class*='product_cat-']"],
    "card_link": ["a.woocommerce-LoopProduct-link::attr(href)",
                  "a.woocommerce-loop-product__link::attr(href)", "a[href*='/product/']::attr(href)"],
    "card_title": ["h2.woocommerce-loop-product__title::text", ".woocommerce-loop-product__title::text",
                   "h2::text", "h3::text"],
    "card_sku": ["a.add_to_cart_button::attr(data-product_sku)", "[data-product_sku]::attr(data-product_sku)"],
    "card_id": ["a.add_to_cart_button::attr(data-product_id)", "[data-product_id]::attr(data-product_id)"],
    "card_img": ["img::attr(src)"],
    "next": ["nav.woocommerce-pagination a.next::attr(href)", "a.next.page-numbers::attr(href)",
             "link[rel='next']::attr(href)"],
    "title": ["h1.product_title::text", "div.summary h1::text", "h1.entry-title::text", "[itemprop='name']::text"],
    "price_block": ["div.summary p.price", "p.price", "div.summary span.price", "[itemprop='offers']"],
    "sku": ["div.product_meta span.sku::text", "span.sku::text", "[itemprop='sku']::text"],
    "categories": ["div.product_meta span.posted_in a[rel='tag']::text", "span.posted_in a::text"],
    "tags": ["div.product_meta span.tagged_as a[rel='tag']::text", "span.tagged_as a::text"],
    "short": ["div.woocommerce-product-details__short-description", "div.summary [itemprop='description']"],
    "description": ["div#tab-description", "div.woocommerce-Tabs-panel--description"],
    "image": ["div.woocommerce-product-gallery__image a::attr(href)",
              "div.woocommerce-product-gallery img::attr(data-large_image)",
              "div.woocommerce-product-gallery img::attr(src)", "img.wp-post-image::attr(src)",
              "meta[property='og:image']::attr(content)"],
}
WS = re.compile(r"\s+")


def norm(text):
    if text is None:
        return None
    text = WS.sub(" ", text.replace("\xa0", " ")).strip()
    return text or None


def first(sel, key):
    for css in SEL[key]:
        v = norm(sel.css(css).get())
        if v:
            return v
    return None


def all_values(sel, key):
    for css in SEL[key]:
        vals = [v for v in (norm(x) for x in sel.css(css).getall()) if v]
        if vals:
            return vals
    return []


def block_text(sel, key):
    for css in SEL[key]:
        node = sel.css(css)
        if node:
            t = norm(" ".join(node.xpath(".//text()[not(ancestor::h2) and not(ancestor::script)]").getall()))
            if t:
                return t
    return None


class EcommerceSpider(scrapy.Spider):
    name = "ecommerce"
    handle_httpstatus_list = [304]

    def __init__(self, start_url=None, max_pages=None, extra_url=None, extra_fail_url=None,
                 extra_outage_s=None, extra_every=1, resume_deferred=False, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_url = start_url or os.environ.get("START_URL") or DEFAULT_START_URL
        self.allowed_domains = [urlparse(self.start_url).hostname]
        for u in (extra_url, extra_fail_url):
            if u and urlparse(u).hostname not in self.allowed_domains:
                self.allowed_domains.append(urlparse(u).hostname)
        self.max_pages = int(max_pages) if max_pages else None
        self.extra_url, self.extra_fail_url = extra_url, extra_fail_url
        self.extra_outage_s = float(extra_outage_s) if extra_outage_s else 0.0
        self.extra_every = max(1, int(extra_every))
        self.resume_deferred = str(resume_deferred).lower() in ("1", "true", "yes")
        self.t0 = time.monotonic()
        self.product_counter = 0

    @property
    def rt(self):
        return get_runtime(self.crawler)

    # ------------------------------------------------------------------ старт
    async def start(self):
        for req in self.start_requests():
            yield req

    def start_requests(self):
        if self.resume_deferred and self.rt.deferred_path.exists():
            saved = json.loads(self.rt.deferred_path.read_text(encoding="utf-8")).get("deferred", [])
            for d in saved:
                if d.get("kind") == "product":
                    yield scrapy.Request(d["url"], callback=self.parse_item, errback=self.on_error,
                                         cb_kwargs={"list_ctx": {}}, meta={"page_kind": "product"},
                                         dont_filter=True, priority=20)
                elif d.get("kind") == "list" and d["url"] != self.start_url:
                    # страница каталога, ждавшая по таймеру в момент прерывания: без этого она теряется —
                    # повторный запрос стартовой страницы найдёт ссылку на неё, но dupefilter (JOBDIR) её отбросит
                    yield scrapy.Request(d["url"], callback=self.parse, errback=self.on_error,
                                         cb_kwargs={"page_no": d.get("page_no") or 2}, meta={"page_kind": "list"},
                                         dont_filter=True, priority=15)
            self.crawler.stats.set_value("lab7/deferred_resumed", len(saved))
        yield scrapy.Request(self.start_url, callback=self.parse, errback=self.on_error,
                             cb_kwargs={"page_no": 1}, meta={"page_kind": "list"}, dont_filter=True)

    # ------------------------------------------------------------------ уровень 1
    def parse(self, response, page_no=1):
        rt, stats = self.rt, self.crawler.stats
        pages = rt.watermark["pages"]
        if response.status == 304:
            saved = pages.get(response.url)
            stats.inc_value("pages/list_not_modified")
            if not saved:                       # нет сохранённых ссылок — запрашиваем страницу целиком
                req = response.request
                yield req.replace(dont_filter=True, meta={"page_kind": "list", "lab7_no_conditional": True})
                return
            cards = [{"href": h, "fp": saved.get("fp", {}).get(h)} for h in saved["links"]]
            next_href = saved.get("next")
        else:
            stats.inc_value("pages/list")
            nodes = []
            for css in SEL["card"]:
                nodes = response.css(css)
                if nodes:
                    break
            cards = []
            for pos, card in enumerate(nodes, start=1):
                href = first(card, "card_link")
                if not href:
                    stats.inc_value("cards/without_link")
                    continue
                href = response.urljoin(href)
                title, price = first(card, "card_title"), norm(" ".join(card.css(".price ::text").getall()))
                fp = hashlib.sha1(f"{title}|{price}|{first(card, 'card_img')}".encode()).hexdigest()[:16]
                cards.append({"href": href, "fp": fp, "title": title, "price": price, "pos": pos,
                              "sku": first(card, "card_sku"), "id": first(card, "card_id")})
            next_href = first(response, "next")
            next_href = response.urljoin(next_href) if next_href else None
            pages[response.url] = {"links": [c["href"] for c in cards], "next": next_href,
                                   "fp": {c["href"]: c["fp"] for c in cards},
                                   "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

        products = rt.watermark["products"]
        now = time.time()
        for c in cards:
            known = products.get(c["href"])
            if (rt.incremental and known and c.get("fp") and known.get("fp") == c["fp"]
                    and now - known.get("checked_at", 0) < rt.revalidate_after_s):
                rt.log("incremental_skip", host=host_of(c["href"]), url=c["href"],
                       reason="отпечаток в каталоге не изменился, проверка свежая")
                stats.inc_value("lab7/incremental_skipped")
                continue
            ctx = {"list_page": response.url, "list_page_no": page_no, "list_position": c.get("pos"),
                   "list_title": c.get("title"), "list_price_raw": c.get("price"), "list_sku": c.get("sku"),
                   "product_id": c.get("id"), "list_fp": c.get("fp")}
            yield scrapy.Request(c["href"], callback=self.parse_item, errback=self.on_error,
                                 cb_kwargs={"list_ctx": ctx}, meta={"page_kind": "product"}, priority=10)
        if next_href and (self.max_pages is None or page_no < self.max_pages):
            yield scrapy.Request(next_href, callback=self.parse, errback=self.on_error,
                                 cb_kwargs={"page_no": page_no + 1}, meta={"page_kind": "list"})

    # ------------------------------------------------------------------ уровень 2
    def parse_item(self, response, list_ctx):
        rt, stats = self.rt, self.crawler.stats
        known = rt.watermark["products"].setdefault(response.url, {})
        known["checked_at"] = time.time()
        if list_ctx.get("list_fp"):
            known["fp"] = list_ctx["list_fp"]
        yield from self._extra_request()
        if response.status == 304:
            stats.inc_value("items/not_modified")       # запись в БД с прошлого прогона актуальна
            return
        ld = self._json_ld(response)
        sku, src = first(response, "sku"), "html"
        if not sku or sku.upper() == "N/A":
            sku, src = norm(str(ld.get("sku") or "")) or None, "jsonld"
            if not sku:
                sku, src = list_ctx.get("list_sku"), "list"
        if not sku or sku.upper() == "N/A":
            sku, src = None, None
        price_raw, regular_raw = self._price(response)
        image = first(response, "image") or (ld.get("image") if isinstance(ld.get("image"), str) else None)
        known["sku"] = sku
        yield ProductItem(
            sku=sku, name=first(response, "title") or norm(ld.get("name")) or list_ctx.get("list_title"),
            price_raw=price_raw, regular_price_raw=regular_raw,
            categories=all_values(response, "categories"), tags=all_values(response, "tags"),
            short_description=block_text(response, "short"),
            description=block_text(response, "description") or norm(ld.get("description")),
            image_url=response.urljoin(image) if image else None, product_url=response.url,
            product_id=list_ctx.get("product_id"), sku_source=src, list_page=list_ctx.get("list_page"),
            list_page_no=list_ctx.get("list_page_no"), list_position=list_ctx.get("list_position"),
            list_title=list_ctx.get("list_title"), list_price_raw=list_ctx.get("list_price_raw"),
            run_id=rt.run_id, scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def _extra_request(self):
        """Запрос ко второму домену (демонстрация изоляции отказа предохранителем)."""
        if not (self.extra_url or self.extra_fail_url):
            return
        self.product_counter += 1
        if self.product_counter % self.extra_every:
            return
        failing = self.extra_fail_url and time.monotonic() - self.t0 < self.extra_outage_s
        url = self.extra_fail_url if failing else (self.extra_url or self.extra_fail_url)
        # URL «аварии» (например, httpbin /status/503) не меняет поведение со временем, поэтому такие
        # запросы при разомкнутом предохранителе не переносятся, а откладываются: пробным запросом
        # после паузы станет уже «здоровый» URL — так видно восстановление closed ← half-open.
        yield scrapy.Request(url, callback=self.parse_extra, errback=self.on_error, dont_filter=True,
                             meta={"page_kind": "extra", "lab7_no_reschedule": bool(failing)}, priority=5)

    def parse_extra(self, response):
        self.crawler.stats.inc_value(f"extra/status_{response.status}")

    @staticmethod
    def _price(response):
        block = None
        for css in SEL["price_block"]:
            found = response.css(css)
            if found:
                block = found[0]
                break
        if block is None:
            return None, None

        def amounts(scope):
            return [v for v in (norm("".join(a.css("::text").getall()))
                                for a in scope.css(".woocommerce-Price-amount")) if v]
        if block.css("ins"):
            cur, reg = amounts(block.css("ins")), amounts(block.css("del"))
        else:
            cur, reg = amounts(block), []
        if not cur:
            return norm(" ".join(block.css("::text").getall())), None
        return " – ".join(cur), (reg[0] if reg else None)

    def _json_ld(self, response):
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

    def on_error(self, failure):
        stats = self.crawler.stats
        if failure.check(HttpError):
            kind = f"http_{failure.value.response.status}"
        elif failure.check(IgnoreRequest):
            kind = {"PolicyReschedule": "rescheduled", "PolicyIgnore": "policy"}.get(
                type(failure.value).__name__, "ignored")
        else:
            kind = type(failure.value).__name__
        stats.inc_value(f"errback/{kind}")
        if kind not in ("policy", "rescheduled"):
            self.logger.warning("Ошибка запроса [%s] %s: %s", kind, failure.request.url, failure.value)
