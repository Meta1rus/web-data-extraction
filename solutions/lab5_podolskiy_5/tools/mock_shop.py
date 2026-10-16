#!/usr/bin/env python3
"""Локальная имитация учебного магазина scrapingcourse.com/ecommerce — ТОЛЬКО для тестов.

Повторяет структуру разметки WooCommerce (тема Storefront), как она описана в
материалах площадки: каталог /ecommerce/ и /ecommerce/page/N/ (16 карточек на
странице), карточки /ecommerce/product/<slug>/. Данные синтетические
(детерминированный генератор), к реальному ассортименту отношения не имеют.

Дополнительно (для лабораторной № 7) умеет воспроизводить отказы:
  * 429 по серверному token bucket (Retry-After в секундах / датой / без заголовка);
  * 503 серией в течение первых N секунд («авария» домена);
  * «тихую» заглушку-капчу с кодом 200;
  * рост латентности в окне запросов (чтобы сработал AutoThrottle);
  * ETag / Last-Modified и ответ 304, gzip, sitemap с lastmod;
  * эндпоинты в стиле httpbin: /status/<код>, /etag/<tag>, /response-headers, /gzip, /delay/<с>.

Служебное: /__mock/stats (счётчики со стороны сервера), /__mock/reset,
/__mock/mutate?n=3 (изменить цену у n товаров первой страницы), /__mock/config?...

Запуск:  python3 tools/mock_shop.py --port 18505 --products 330
Только стандартная библиотека, Python >= 3.10.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import random
import re
import threading
import time
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PER_PAGE = 16
BASE_PATH = "/ecommerce/"

FIRST = [
    "Abominable", "Adrienne", "Aeon", "Aero", "Affirm", "Agasa", "Aim", "Ajax", "Antonia",
    "Apollo", "Arcadio", "Argus", "Ariel", "Artemis", "Atlas", "Atomic", "Augusta", "Autumn",
    "Ava", "Balboa", "Bella", "Breathe-Easy", "Bruno", "Caesar", "Carina", "Cassia", "Cassius",
    "Chaz", "Chloe", "Circe", "Cora", "Cronus", "Cruise", "Daphne", "Deion", "Desiree", "Diana",
    "Didi", "Dione", "Echo", "Electra", "Elisa", "Emma", "Erica", "Erika", "Eos", "Fiona",
    "Frankie", "Gabrielle", "Geo", "Gobi", "Gwen", "Gwyn", "Hawkeye", "Hera", "Hero",
    "Hollister", "Hyperion", "Ida", "Iliana", "Inez", "Ingrid", "Jade", "Josie", "Juliana",
    "Juno", "Jupiter", "Karissa", "Karmen", "Kenobi", "Kratos", "Layla", "Lando", "Leah",
    "Leona", "Livingston", "Lono", "Lucia", "Mach", "Marco", "Mars", "Maya", "Meteor",
    "Minerva", "Miko", "Mimi", "Nadia", "Neve", "Nora", "Olivia", "Orestes", "Orion", "Phoebe",
    "Portia", "Primo", "Prima", "Proteus", "Radiant", "Rapha", "Riona", "Rocco", "Sahara",
    "Selene", "Sinbad", "Sparta", "Stellar", "Strike", "Sybil", "Taurus", "Teton", "Thorpe",
    "Tiberius", "Tristan", "Troy", "Typhon", "Ulysses", "Vulcan", "Wayfarer", "Zeppelin",
    "Zing", "Zoe", "Zoltan",
]
TYPES = [  # (тип, категории, префикс артикула)
    ("Hoodie", ["Hoodies & Sweatshirts", "Men", "Tops"], "MH"),
    ("Sweatshirt", ["Hoodies & Sweatshirts", "Women", "Tops"], "WH"),
    ("Trek Jacket", ["Jackets", "Women", "Tops"], "WJ"),
    ("Running Jacket", ["Jackets", "Men", "Tops"], "MJ"),
    ("Gym Tee", ["Tees", "Men", "Tops"], "MS"),
    ("Tank", ["Bras & Tanks", "Women", "Tops"], "WT"),
    ("Capri", ["Pants", "Women", "Bottoms"], "WP"),
    ("Running Short", ["Shorts", "Men", "Bottoms"], "MSH"),
    ("Yoga Pant", ["Pants", "Men", "Bottoms"], "MP"),
    ("Backpack", ["Bags", "Gear"], "24-MB"),
    ("Sport Watch", ["Watches", "Gear"], "24-MG"),
    ("Foam Roller", ["Fitness Equipment", "Gear"], "24-UG"),
]
PHRASES = [
    "Lightweight fabric keeps you cool during intense workouts.",
    "Moisture-wicking technology and flatlock seams for comfort.",
    "Designed for runners who log serious miles.",
    "A relaxed fit that layers easily over training gear.",
    "Recycled materials and a durable, colorfast finish.",
    "Reflective details improve visibility in low light.",
    "Machine wash cold, tumble dry low.",
    "Ergonomic shape distributes weight evenly.",
]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def build_catalog(n: int, seed: int = 5) -> list[dict]:
    """Детерминированный синтетический каталог + специально подготовленные «трудные» карточки."""
    rnd = random.Random(seed)
    pairs = [(f, t) for t in TYPES for f in FIRST]
    rnd.shuffle(pairs)
    counters: dict[str, int] = {}
    base_ts = 1_735_689_600  # 2025-01-01 UTC — «дата изменения» товаров
    products = []
    for i in range(n):
        first, (tname, cats, prefix) = pairs[i % len(pairs)]
        counters[prefix] = counters.get(prefix, 0) + 1
        sku = f"{prefix}{counters[prefix]:02d}"
        name = f"{first} {tname}"
        price = rnd.choice([19, 22, 24, 29, 32, 34, 39, 42, 45, 49, 52, 54, 57, 59, 62, 64,
                            66, 69, 74, 77, 79, 84, 89, 92, 99]) + rnd.choice([0, 0, 0, 0, 0.5, 0.99])
        p = {
            "id": 100 + i, "name": name, "slug": slugify(name), "sku": sku,
            "price": f"{price:.2f}", "regular": None, "price_max": None,
            "categories": list(cats), "tags": [tname.split()[-1].lower(), first.lower()],
            "short": rnd.choice(PHRASES),
            "description": " ".join(rnd.sample(PHRASES, 3)),
            "image": f"/ecommerce/wp-content/uploads/2024/03/{sku.lower()}_main.jpg",
            "modified": base_ts + i * 3600,
            "sku_in_html": True, "sku_in_jsonld": True, "case": None,
        }
        r = rnd.random()
        if r < 0.10:                              # распродажа: старая и новая цена
            p["regular"] = f"{price + rnd.choice([5, 8, 10, 15]):.2f}"
        elif r < 0.17:                            # вариативный товар: диапазон цен
            p["price_max"] = f"{price + rnd.choice([4, 7, 11]):.2f}"
        products.append(p)

    # Специальные случаи (номера фиксированы, чтобы тесты были воспроизводимы)
    def case(idx: int, label: str, **changes):
        if idx < len(products):
            products[idx].update(changes)
            products[idx]["case"] = label

    case(5, "E1_no_sku", sku_in_html=False, sku_in_jsonld=False, sku="")
    case(21, "E2_sku_na", sku_in_html=True, sku_in_jsonld=False, sku="N/A")
    case(37, "E3_sku_only_jsonld", sku_in_html=False, sku_in_jsonld=True)
    case(53, "E5_bad_price", price="Price on request", regular=None, price_max=None)
    case(69, "E6_no_description", short="", description="")
    case(85, "E7_placeholder_image", image="/ecommerce/wp-content/uploads/woocommerce-placeholder.png")
    if len(products) > 101:
        products[101]["name"] = "  Didi&nbsp;Sport   Watch  "
        products[101]["case"] = "E8_dirty_title"
    if len(products) > 120:                       # E4: дубль артикула под другим URL
        dup = dict(products[10])
        dup.update(id=99_000, slug=products[10]["slug"] + "-2", case="E4_duplicate_sku",
                   modified=products[10]["modified"] + 60)
        products.insert(120, dup)
    return products


# --------------------------------------------------------------------------- HTML
def money(value: str) -> str:
    return ('<span class="woocommerce-Price-amount amount"><bdi>'
            '<span class="woocommerce-Price-currencySymbol">&#36;</span>'
            f'{value}</bdi></span>')


def price_html(p: dict) -> str:
    if not re.match(r"^\d", p["price"]):
        return html.escape(p["price"])
    if p["regular"]:
        return (f'<del aria-hidden="true">{money(p["regular"])}</del> '
                f'<span class="screen-reader-text">Original price was: ${p["regular"]}.</span>'
                f'<ins aria-hidden="true">{money(p["price"])}</ins>'
                f'<span class="screen-reader-text">Current price is: ${p["price"]}.</span>')
    if p["price_max"]:
        return f'{money(p["price"])} <span aria-hidden="true">&ndash;</span> {money(p["price_max"])}'
    return money(p["price"])


def page_shell(origin: str, title: str, body: str, body_class: str, canonical: str = "") -> str:
    canon = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return f"""<!DOCTYPE html>
<html lang="en-US">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} &#8211; ScrapingCourse.com E-commerce (local mock)</title>
<link rel="stylesheet" href="{origin}/ecommerce/wp-content/themes/storefront/style.css">
{canon}
</head>
<body class="{body_class} theme-storefront woocommerce woocommerce-page">
<div id="page" class="hfeed site">
<header id="masthead" class="site-header" role="banner"><div class="col-full">
<div class="site-branding"><a href="{origin}/ecommerce/" class="custom-logo-link" rel="home">ScrapingCourse.com</a></div>
</div></header>
<div id="content" class="site-content"><div class="col-full">
<div id="primary" class="content-area"><main id="main" class="site-main" role="main">
{body}
</main></div>
</div></div>
<footer id="colophon" class="site-footer"><div class="col-full">&copy; local mock for tests</div></footer>
</div>
</body>
</html>
"""


def render_list(origin: str, products: list[dict], page: int) -> str:
    total = len(products)
    pages = max(1, math.ceil(total / PER_PAGE))
    chunk = products[(page - 1) * PER_PAGE: page * PER_PAGE]
    lis = []
    for k, p in enumerate(chunk):
        pos_cls = " first" if k % 4 == 0 else (" last" if k % 4 == 3 else "")
        cat_cls = " ".join("product_cat-" + slugify(c) for c in p["categories"])
        img = f'{origin}{p["image"]}'.replace("_main.jpg", "_main-324x324.jpg")
        sku_attr = html.escape(p["sku"]) if p["sku_in_html"] and p["sku"] not in ("", "N/A") else ""
        ptype = "variable" if p["price_max"] else "simple"
        lis.append(f"""<li class="product type-product post-{p['id']} status-publish{pos_cls} instock {cat_cls} has-post-thumbnail taxable shipping-taxable purchasable product-type-{ptype}">
<a href="{origin}/ecommerce/product/{p['slug']}/" class="woocommerce-LoopProduct-link woocommerce-loop-product__link"><img width="324" height="324" src="{img}" class="attachment-woocommerce_thumbnail size-woocommerce_thumbnail" alt="" decoding="async"><h2 class="woocommerce-loop-product__title">{p['name']}</h2>
<span class="price">{price_html(p)}</span>
</a><a href="?add-to-cart={p['id']}" data-quantity="1" class="button wp-element-button product_type_{ptype} add_to_cart_button ajax_add_to_cart" data-product_id="{p['id']}" data-product_sku="{sku_attr}" aria-label="Add to cart: &ldquo;{html.escape(p['name'].strip())}&rdquo;" rel="nofollow">Add to cart</a></li>""")
    nav = []
    for n in range(1, pages + 1):
        if n == page:
            nav.append(f'<li><span aria-current="page" class="page-numbers current">{n}</span></li>')
        else:
            href = f"{origin}/ecommerce/" if n == 1 else f"{origin}/ecommerce/page/{n}/"
            nav.append(f'<li><a class="page-numbers" href="{href}">{n}</a></li>')
    if page < pages:
        nav.append(f'<li><a class="next page-numbers" href="{origin}/ecommerce/page/{page + 1}/">&rarr;</a></li>')
    if page > 1:
        prev = f"{origin}/ecommerce/" if page == 2 else f"{origin}/ecommerce/page/{page - 1}/"
        nav.insert(0, f'<li><a class="prev page-numbers" href="{prev}">&larr;</a></li>')
    first_i = (page - 1) * PER_PAGE + 1
    last_i = (page - 1) * PER_PAGE + len(chunk)
    body = f"""<header class="woocommerce-products-header"><h1 class="woocommerce-products-header__title page-title">Shop</h1></header>
<div class="storefront-sorting"><p class="woocommerce-result-count" role="alert" aria-relevant="all">Showing {first_i}&ndash;{last_i} of {total} results</p></div>
<ul class="products columns-4">
{chr(10).join(lis)}
</ul>
<nav class="woocommerce-pagination" aria-label="Product Pagination"><ul class="page-numbers">
{''.join(nav)}
</ul></nav>"""
    canonical = f"{origin}/ecommerce/" if page == 1 else f"{origin}/ecommerce/page/{page}/"
    return page_shell(origin, "Shop" if page == 1 else f"Shop – Page {page}", body,
                      "archive post-type-archive post-type-archive-product", canonical)


def render_product(origin: str, p: dict) -> str:
    cats = ", ".join(f'<a href="{origin}/ecommerce/product-category/{slugify(c)}/" rel="tag">{html.escape(c)}</a>'
                     for c in p["categories"])
    tags = ", ".join(f'<a href="{origin}/ecommerce/product-tag/{t}/" rel="tag">{t}</a>' for t in p["tags"])
    sku_html = ""
    if p["sku_in_html"]:
        sku_html = f'<span class="sku_wrapper">SKU: <span class="sku">{html.escape(p["sku"]) or "N/A"}</span></span>\n'
    full_img = f'{origin}{p["image"]}'
    img600 = full_img.replace("_main.jpg", "_main-600x600.jpg")
    short = (f'<div class="woocommerce-product-details__short-description"><p>{p["short"]}</p></div>'
             if p["short"] else "")
    tabs = ""
    if p["description"]:
        tabs = f"""<div class="woocommerce-tabs wc-tabs-wrapper">
<ul class="tabs wc-tabs" role="tablist"><li class="description_tab active" id="tab-title-description"><a href="#tab-description">Description</a></li>
<li class="additional_information_tab" id="tab-title-additional_information"><a href="#tab-additional_information">Additional information</a></li></ul>
<div class="woocommerce-Tabs-panel woocommerce-Tabs-panel--description panel entry-content wc-tab" id="tab-description" role="tabpanel" aria-labelledby="tab-title-description">
<h2>Description</h2>
<p>{p["description"]}</p>
</div>
<div class="woocommerce-Tabs-panel woocommerce-Tabs-panel--additional_information panel entry-content wc-tab" id="tab-additional_information" role="tabpanel">
<h2>Additional information</h2>
<table class="woocommerce-product-attributes shop_attributes"><tr class="woocommerce-product-attributes-item"><th class="woocommerce-product-attributes-item__label">Material</th><td class="woocommerce-product-attributes-item__value"><p>Cotton, Polyester</p></td></tr></table>
</div>
</div>"""
    ld: dict = {"@context": "https://schema.org/", "@type": "Product", "name": html.unescape(p["name"]).strip(),
                "url": f"{origin}/ecommerce/product/{p['slug']}/", "image": full_img,
                "description": p["short"] or p["description"]}
    if p["sku_in_jsonld"] and p["sku"] not in ("", "N/A"):
        ld["sku"] = p["sku"]
    if re.match(r"^\d", p["price"]):
        ld["offers"] = [{"@type": "Offer", "price": p["price"], "priceCurrency": "USD",
                         "availability": "http://schema.org/InStock"}]
    cat_cls = " ".join("product_cat-" + slugify(c) for c in p["categories"])
    body = f"""<nav class="woocommerce-breadcrumb" aria-label="Breadcrumb"><a href="{origin}/ecommerce/">Home</a>&nbsp;&#47;&nbsp;<a href="{origin}/ecommerce/product-category/{slugify(p['categories'][0])}/">{html.escape(p['categories'][0])}</a>&nbsp;&#47;&nbsp;{p['name']}</nav>
<div class="woocommerce-notices-wrapper"></div>
<div id="product-{p['id']}" class="product type-product post-{p['id']} status-publish first instock {cat_cls} has-post-thumbnail taxable shipping-taxable purchasable product-type-simple">
<div class="woocommerce-product-gallery woocommerce-product-gallery--with-images woocommerce-product-gallery--columns-4 images" data-columns="4">
<div class="woocommerce-product-gallery__wrapper">
<div data-thumb="{full_img.replace('_main.jpg', '_main-100x100.jpg')}" class="woocommerce-product-gallery__image"><a href="{full_img}"><img width="600" height="600" src="{img600}" class="wp-post-image" alt="" title="{p['slug']}" data-caption="" data-src="{full_img}" data-large_image="{full_img}" data-large_image_width="1000" data-large_image_height="1000" decoding="async"></a></div>
</div>
</div>
<div class="summary entry-summary">
<h1 class="product_title entry-title">{p['name']}</h1>
<p class="price">{price_html(p)}</p>
{short}
<form class="cart" action="{origin}/ecommerce/product/{p['slug']}/" method="post" enctype="multipart/form-data"><div class="quantity"><input type="number" class="input-text qty text" name="quantity" value="1" min="1"></div><button type="submit" name="add-to-cart" value="{p['id']}" class="single_add_to_cart_button button alt">Add to cart</button></form>
<div class="product_meta">
{sku_html}<span class="posted_in">Categories: {cats}</span>
<span class="tagged_as">Tags: {tags}</span>
</div>
</div>
{tabs}
</div>
<script type="application/ld+json">{json.dumps(ld)}</script>"""
    return page_shell(origin, html.unescape(p["name"]).strip(), body,
                      f"product-template-default single single-product postid-{p['id']}",
                      f"{origin}/ecommerce/product/{p['slug']}/")


CAPTCHA_HTML = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><h1>Access denied</h1><p>Please complete the CAPTCHA to continue. Are you human?</p>
<div class="cf-browser-verification"></div></body></html>"""

ROBOTS = {
    "open": ("User-agent: *\nDisallow: /ecommerce/cart/\nDisallow: /ecommerce/checkout/\n"
             "Disallow: /ecommerce/my-account/\nDisallow: /*?add-to-cart=\n"
             "Crawl-delay: {crawl_delay}\n\nSitemap: {origin}/wp-sitemap.xml\n"),
    # Ровно то содержимое, что отдаёт настоящий www.scrapingcourse.com/robots.txt (проверено 15.10.2026)
    "real": "User-agent: *\nDisallow: /ecommerce/*\n",
}


# --------------------------------------------------------------------------- server state
class State:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.products = build_catalog(args.products)
        self.by_slug = {p["slug"]: p for p in self.products}
        self.started = time.monotonic()
        self.first_hit: float | None = None
        self.config = {
            "ratelimit": args.ratelimit,            # "R:B" или ""
            "retry_after_mode": args.retry_after_mode,
            "outage_seconds": args.outage_seconds,
            "captcha_paths": args.captcha_paths,
            "slow_from": args.slow_from, "slow_to": args.slow_to, "slow_ms": args.slow_ms,
            "latency_ms": args.latency_ms, "robots": args.robots, "crawl_delay": args.crawl_delay,
        }
        self.reset_stats()
        self.bucket_tokens: dict[str, float] = {}
        self.bucket_ts: dict[str, float] = {}
        self.ra_cycle = 0

    def reset_stats(self):
        self.stats = {"requests": 0, "by_status": {}, "bytes_sent": 0, "arrivals": [],
                      "not_modified": 0, "gzip_responses": 0, "rate_limited": 0}
        self.n = 0

    def take_token(self, client: str) -> tuple[bool, float]:
        spec = self.config["ratelimit"]
        if not spec:
            return True, 0.0
        rate, burst = (float(x) for x in spec.split(":"))
        now = time.monotonic()
        tokens = self.bucket_tokens.get(client, burst)
        tokens = min(burst, tokens + (now - self.bucket_ts.get(client, now)) * rate)
        self.bucket_ts[client] = now
        if tokens >= 1:
            self.bucket_tokens[client] = tokens - 1
            return True, 0.0
        self.bucket_tokens[client] = tokens
        return False, (1 - tokens) / rate


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockWooCommerce/1.0"
    state: State  # назначается в main()

    def log_message(self, fmt, *args):  # тихий режим: журнал ведёт сам краулер
        if self.state.args.verbose:
            super().log_message(fmt, *args)

    # ---- helpers
    def origin(self) -> str:
        return f"http://{self.headers.get('Host') or 'localhost'}"

    def send(self, status: int, body: bytes | str, ctype="text/html; charset=UTF-8",
             headers: dict | None = None, etag: str | None = None, last_modified: float | None = None):
        st = self.state
        if isinstance(body, str):
            body = body.encode("utf-8")
        hdrs = dict(headers or {})
        if etag:
            hdrs["ETag"] = etag
        if last_modified is not None:
            hdrs["Last-Modified"] = formatdate(last_modified, usegmt=True)
        if status == 200 and (etag or last_modified is not None):
            inm = self.headers.get("If-None-Match")
            ims = self.headers.get("If-Modified-Since")
            not_modified = False
            if inm and etag:
                not_modified = etag in [t.strip() for t in inm.split(",")]
            elif ims and last_modified is not None:
                try:
                    not_modified = parsedate_to_datetime(ims).timestamp() >= int(last_modified)
                except (TypeError, ValueError):
                    not_modified = False
            if not_modified:
                status, body = 304, b""
                with st.lock:
                    st.stats["not_modified"] += 1
        if body and "gzip" in (self.headers.get("Accept-Encoding") or "") and status != 304:
            body = gzip.compress(body, 6)
            hdrs["Content-Encoding"] = "gzip"
            hdrs["Vary"] = "Accept-Encoding"
            with st.lock:
                st.stats["gzip_responses"] += 1
        self.send_response(status)
        if status != 304:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        with st.lock:
            st.stats["by_status"][str(status)] = st.stats["by_status"].get(str(status), 0) + 1
            st.stats["bytes_sent"] += len(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):  # noqa: C901 — маршрутизация имитации
        st = self.state
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        cfg = st.config
        with st.lock:
            st.n += 1
            n = st.n
            st.stats["requests"] += 1
            now = time.monotonic()
            st.stats["arrivals"].append(round(now - st.started, 4))
            if path not in ("/robots.txt",) and not path.startswith("/__mock") and st.first_hit is None:
                st.first_hit = now

        if path.startswith("/__mock/"):
            return self.admin(path, query)

        # латентность (база + окно «медленного» сервера)
        delay_ms = cfg["latency_ms"] * (0.7 + 0.6 * random.random())
        if cfg["slow_from"] <= n < cfg["slow_to"]:
            delay_ms += cfg["slow_ms"]
        time.sleep(delay_ms / 1000.0)

        if path == "/robots.txt":
            mode = cfg["robots"]
            if mode == "none":
                return self.send(404, "<h1>404 Not Found</h1>")
            text = ROBOTS[mode].format(origin=self.origin(), crawl_delay=cfg["crawl_delay"])
            return self.send(200, text, ctype="text/plain; charset=utf-8")

        # серверный ограничитель скорости → 429
        client = self.client_address[0]
        with st.lock:
            ok, wait = st.take_token(client)
            if not ok:
                st.stats["rate_limited"] += 1
                mode = cfg["retry_after_mode"]
                if mode == "cycle":
                    mode = ("seconds", "date", "none")[st.ra_cycle % 3]
                    st.ra_cycle += 1
        if not ok:
            hdr = {}
            secs = max(1, math.ceil(wait))
            if mode == "seconds":
                hdr["Retry-After"] = str(secs)
            elif mode == "date":
                hdr["Retry-After"] = formatdate(time.time() + secs + 1, usegmt=True)
            return self.send(429, "<h1>429 Too Many Requests</h1>", headers=hdr)

        # «авария»: 503 в течение первых N секунд после первого обращения
        if cfg["outage_seconds"] and st.first_hit is not None \
                and time.monotonic() - st.first_hit < cfg["outage_seconds"]:
            return self.send(503, "<h1>503 Service Unavailable</h1>")

        if cfg["captcha_paths"] and re.search(cfg["captcha_paths"], path):
            return self.send(200, CAPTCHA_HTML)

        # httpbin-подобные эндпоинты
        m = re.fullmatch(r"/status/(\d{3})", path)
        if m:
            code = int(m.group(1))
            hdr = {}
            if "retry_after" in query:
                hdr["Retry-After"] = query["retry_after"][0]
            return self.send(code, "" if code in (204, 304) else f"status {code}", ctype="text/plain", headers=hdr)
        m = re.fullmatch(r"/etag/([^/]+)", path)
        if m:
            body = json.dumps({"tag": m.group(1), "note": "httpbin-like etag endpoint"}).encode() * 20
            return self.send(200, body, ctype="application/json", etag=f'"{m.group(1)}"')
        if path == "/response-headers":
            return self.send(200, json.dumps({k: v[0] for k, v in query.items()}),
                             ctype="application/json", headers={k: v[0] for k, v in query.items()})
        if path == "/gzip":
            return self.send(200, json.dumps({"gzipped": True, "pad": "x" * 4000}), ctype="application/json")
        m = re.fullmatch(r"/delay/(\d+(?:\.\d+)?)", path)
        if m:
            time.sleep(min(float(m.group(1)), 30))
            return self.send(200, json.dumps({"delay": m.group(1)}), ctype="application/json")

        if path in ("/wp-sitemap.xml",):
            body = (f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f'<sitemap><loc>{self.origin()}/wp-sitemap-posts-product-1.xml</loc></sitemap></sitemapindex>')
            return self.send(200, body, ctype="application/xml")
        if path == "/wp-sitemap-posts-product-1.xml":
            urls = "".join(f"<url><loc>{self.origin()}/ecommerce/product/{p['slug']}/</loc>"
                           f"<lastmod>{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(p['modified']))}</lastmod></url>"
                           for p in st.products)
            return self.send(200, f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
                             ctype="application/xml")

        # каталог
        m = re.fullmatch(r"/ecommerce/?(?:page/(\d+)/?)?", path)
        if m:
            page = int(m.group(1) or 1)
            pages = max(1, math.ceil(len(st.products) / PER_PAGE))
            if page > pages:
                return self.send(404, page_shell(self.origin(), "Page not found", "<h1>Oops! That page can&rsquo;t be found.</h1>", "error404"))
            body = render_list(self.origin(), st.products, page)
            chunk = st.products[(page - 1) * PER_PAGE: page * PER_PAGE]
            lm = max(p["modified"] for p in chunk)
            return self.send(200, body, etag='"' + hashlib.sha1(body.encode()).hexdigest()[:16] + '"', last_modified=lm)
        m = re.fullmatch(r"/ecommerce/product/([a-z0-9-]+)/?", path)
        if m:
            p = st.by_slug.get(m.group(1))
            if not p:
                return self.send(404, page_shell(self.origin(), "Page not found", "<h1>Oops! That page can&rsquo;t be found.</h1>", "error404"))
            body = render_product(self.origin(), p)
            return self.send(200, body, etag='"' + hashlib.sha1(body.encode()).hexdigest()[:16] + '"', last_modified=p["modified"])
        return self.send(404, page_shell(self.origin(), "Page not found", "<h1>Oops! That page can&rsquo;t be found.</h1>", "error404"))

    def admin(self, path, query):
        st = self.state
        if path == "/__mock/stats":
            with st.lock:
                data = json.loads(json.dumps(st.stats))
                if query.get("reset"):
                    st.reset_stats()
            return self.send(200, json.dumps(data), ctype="application/json")
        if path == "/__mock/reset":
            with st.lock:
                st.reset_stats()
                st.first_hit = None
                st.bucket_tokens.clear()
                st.bucket_ts.clear()
            return self.send(200, "{}", ctype="application/json")
        if path == "/__mock/mutate":
            n = int(query.get("n", ["3"])[0])
            changed = []
            with st.lock:
                for p in st.products[:PER_PAGE][:n]:
                    if re.match(r"^\d", p["price"]):
                        p["price"] = f"{float(p['price']) + 1:.2f}"
                        p["modified"] += 86_400
                        changed.append(p["slug"])
            return self.send(200, json.dumps({"changed": changed}), ctype="application/json")
        if path == "/__mock/config":
            with st.lock:
                for k, v in query.items():
                    if k in st.config:
                        cur = st.config[k]
                        st.config[k] = type(cur)(v[0]) if cur is not None and not isinstance(cur, str) else v[0]
                if "outage_seconds" in query:
                    st.first_hit = None
                data = dict(st.config)
            return self.send(200, json.dumps(data), ctype="application/json")
        if path == "/__mock/info":
            cases = [{"slug": p["slug"], "case": p["case"], "sku": p["sku"]} for p in st.products if p["case"]]
            return self.send(200, json.dumps({"products": len(st.products),
                                              "pages": math.ceil(len(st.products) / PER_PAGE),
                                              "special_cases": cases}, ensure_ascii=False),
                             ctype="application/json")
        return self.send(404, "unknown admin endpoint", ctype="text/plain")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18505)
    ap.add_argument("--products", type=int, default=330, help="число товаров (по умолчанию 330 → 21 страница)")
    ap.add_argument("--robots", choices=["open", "real", "none"], default="open",
                    help="open — разрешающий файл; real — копия настоящего (Disallow: /ecommerce/*); none — 404")
    ap.add_argument("--crawl-delay", type=float, default=0.2)
    ap.add_argument("--latency-ms", type=float, default=15)
    ap.add_argument("--ratelimit", default="", help='серверный лимит "rate:burst", например "4:4"; пусто — без лимита')
    ap.add_argument("--retry-after-mode", choices=["seconds", "date", "none", "cycle"], default="seconds")
    ap.add_argument("--outage-seconds", type=float, default=0.0)
    ap.add_argument("--captcha-paths", default="", help="регулярное выражение путей, где отдать капчу с кодом 200")
    ap.add_argument("--slow-from", type=int, default=10**9)
    ap.add_argument("--slow-to", type=int, default=10**9)
    ap.add_argument("--slow-ms", type=float, default=0.0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    Handler.state = State(args)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    pages = math.ceil(len(Handler.state.products) / PER_PAGE)
    print(f"mock shop: http://{args.host}:{args.port}/ecommerce/ — {len(Handler.state.products)} товаров, "
          f"{pages} страниц, robots={args.robots}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("mock shop: остановлен по Ctrl+C", flush=True)
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
