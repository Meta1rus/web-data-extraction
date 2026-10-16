"""Настройки Scrapy-краулера лабораторной № 7 (база — краулер лабораторной № 5).

Здесь — режим B («вежливый») по умолчанию. Режимы A/B/C, параметры ограничителя,
повторов и предохранителя задаёт src/collect.py через словарь LAB7 и переопределения
ниже (см. MODE_OVERRIDES в collect.py).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BOT_NAME = "resilient_crawler"
SPIDER_MODULES = ["src.crawler.spiders"]
NEWSPIDER_MODULE = "src.crawler.spiders"

USER_AGENT = "PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)"
ROBOTSTXT_OBEY = True                  # во всех режимах, включая «наивный» A

CONCURRENT_REQUESTS = 8
CONCURRENT_REQUESTS_PER_DOMAIN = 2
# Скорость задаёт наш token bucket (src/limiter.py) в middleware; встроенная пауза Scrapy = 0,
# а AutoThrottle остаётся включённым как вторая, «серверная» обратная связь по латентности.
DOWNLOAD_DELAY = 0
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 0.5
AUTOTHROTTLE_MAX_DELAY = 10.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
DOWNLOAD_TIMEOUT = 20
DOWNLOAD_MAXSIZE = 5 * 1024 * 1024

# Встроенный RetryMiddleware выключен: повторы делает src/retry.py (классы ошибок,
# full jitter, cap, бюджет, Retry-After).
RETRY_ENABLED = False
COMPRESSION_ENABLED = False            # gzip включается в режиме C (замер экономии)
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False

DEPTH_LIMIT = 60
CLOSESPIDER_TIMEOUT = 1800
CLOSESPIDER_ERRORCOUNT = 20

DOWNLOADER_MIDDLEWARES = {
    # 545: после распаковки (590) — детектор видит тело ответа как есть
    "src.crawler.middlewares.BlockDetectorMiddleware": 545,
    # 830/840: до DownloaderStats (850) — перенесённые/отклонённые политикой запросы не попадают
    # в downloader/request_count; process_response видит тело ДО распаковки (590) — байты «на проводе»
    "src.crawler.middlewares.ConditionalRequestMiddleware": 830,
    "src.crawler.middlewares.ResilienceMiddleware": 840,
}
EXTENSIONS = {
    "src.crawler.extensions.RunObserver": 500,
}
ITEM_PIPELINES = {
    "src.crawler.pipelines.ValidationPipeline": 100,
    "src.crawler.pipelines.CleanPipeline": 200,
    "src.crawler.pipelines.DedupPipeline": 300,
    "src.crawler.pipelines.SQLitePipeline": 400,
}
REQUIRED_FIELDS = ["sku", "name", "price_raw", "product_url"]
MONITORED_FIELDS = ["name", "price_raw", "sku", "categories", "description", "image_url"]
SQLITE_PATH = str(ROOT / "data" / "crawl.db")
SQLITE_BATCH_SIZE = 50

LOG_LEVEL = "WARNING"                  # человеку — только предупреждения; метрики — в JSON-журнале
FEED_EXPORT_ENCODING = "utf-8"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

LAB7: dict = {}                        # заполняет src/collect.py
