"""Настройки Scrapy-проекта ecommerce_crawler (лабораторная № 5, вариант 5).

Каждое значение снабжено обоснованием. Любую настройку можно переопределить
при запуске: scrapy crawl ecommerce -s CLOSESPIDER_ITEMCOUNT=50
Пути к файлам заданы от корня лабораторной, поэтому запуск возможен из любого каталога.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

BOT_NAME = "ecommerce_crawler"
SPIDER_MODULES = ["ecommerce_crawler.spiders"]
NEWSPIDER_MODULE = "ecommerce_crawler.spiders"

# --- Вежливость -----------------------------------------------------------------------
# Честный User-Agent с назначением и контактом: администратор сайта может написать нам,
# а не блокировать вслепую. Подмена под браузер запрещена правилами курса.
USER_AGENT = "PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)"

# robots.txt соблюдается всегда (False — штраф −15 и нарушение правил площадки).
# Парсер по умолчанию — Protego: понимает шаблоны * и $ (RFC 9309).
ROBOTSTXT_OBEY = True

# Глобальный потолок параллельных запросов: краулер ходит на один домен, 8 достаточно,
# чтобы очередь не простаивала, пока ограничение на домен держит нагрузку низкой.
CONCURRENT_REQUESTS = 8
# Не более 2 одновременных запросов к одному домену (требование задания: ≤ 4).
# Учебная площадка — общий ресурс для многих студентов, поэтому берём половину лимита.
CONCURRENT_REQUESTS_PER_DOMAIN = 2

# Базовая пауза между запросами к домену, с. Она же — нижняя граница для AutoThrottle.
# 0.5 с × 2 потока ≈ не более 4 запросов/с в пике; при ~200 страницах — около 1–2 минут.
DOWNLOAD_DELAY = 0.5
# Случайный разброс паузы ±50 % (0.25–0.75 с): нет «машинного» ритма запросов.
# В Scrapy ≥ 2.19 это DOWNLOAD_DELAY_JITTER; в старых версиях то же самое делает
# RANDOMIZE_DOWNLOAD_DELAY, включённый по умолчанию.
DOWNLOAD_DELAY_JITTER = 0.5

# AutoThrottle подстраивает паузу под латентность сервера: медленнее ответы → реже запросы.
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0         # стартуем осторожно, пока латентность не измерена
AUTOTHROTTLE_MAX_DELAY = 10.0          # потолок паузы при деградации сервера
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0  # в среднем один запрос «в полёте» к серверу
AUTOTHROTTLE_DEBUG = False             # True — печатать каждую корректировку задержки

# Таймаут ответа, с: страницы каталога весят десятки КБ; 20 с с запасом покрывают медленный
# ответ, но не дают зависшему соединению держать слот бесконечно.
DOWNLOAD_TIMEOUT = 20
DOWNLOAD_MAXSIZE = 5 * 1024 * 1024     # предохранитель от неожиданно больших ответов

# Повторы только для временных сбоев: 2 повтора (итого 3 попытки). 4xx (кроме 408/429)
# не повторяем — это ошибка запроса, а не сервера.
RETRY_ENABLED = True
RETRY_TIMES = 2
RETRY_HTTP_CODES = [408, 429, 500, 502, 503, 504, 522, 524]

COOKIES_ENABLED = False                # сессия не нужна: каталог публичный, без авторизации
TELNETCONSOLE_ENABLED = False

# --- Предохранители ---------------------------------------------------------------------
# Глубина: страница каталога N находится на глубине N-1, карточка — на глубине N.
# В каталоге ≤ 48 страниц (у самого крупного магазина-кандидата), поэтому 60 — запас,
# который одновременно останавливает «бесконечную» пагинацию при ошибке вёрстки.
DEPTH_LIMIT = 60
DEPTH_STATS_VERBOSE = True             # распределение запросов по глубине — в статистику

# Итоговый прогон: остановка после 400 записей (требование ≥ 300 + запас на отбраковку).
CLOSESPIDER_ITEMCOUNT = 400
CLOSESPIDER_PAGECOUNT = 700            # не более 700 ответов за прогон
CLOSESPIDER_TIMEOUT = 1800             # не дольше 30 минут
CLOSESPIDER_ERRORCOUNT = 20            # 20 исключений в колбэках — что-то сломалось, стоп

# --- HTTP-кэш (только для отладки) ----------------------------------------------------------
# В итоговом прогоне выключен: нужны свежие данные. При отладке селекторов включается
# ключом -s HTTPCACHE_ENABLED=True — повторные запуски не обращаются к сайту вовсе.
HTTPCACHE_ENABLED = False
HTTPCACHE_DIR = str(PROJECT_ROOT / ".scrapy" / "httpcache")
HTTPCACHE_EXPIRATION_SECS = 86400      # сутки: на время отладки этого достаточно
HTTPCACHE_IGNORE_HTTP_CODES = [403, 429, 500, 502, 503, 504]  # ошибки не кэшируем

# --- Журнал -----------------------------------------------------------------------------------
LOG_LEVEL = "INFO"                     # INFO: видны итоговая статистика и предупреждения
LOGSTATS_INTERVAL = 30.0

# --- Пайплайн и хранилище ----------------------------------------------------------------------
ITEM_PIPELINES = {
    "ecommerce_crawler.pipelines.ValidationPipeline": 100,
    "ecommerce_crawler.pipelines.CleanPipeline": 200,
    "ecommerce_crawler.pipelines.DedupPipeline": 300,
    "ecommerce_crawler.pipelines.SQLitePipeline": 400,
}
# Поля, без которых запись бессмысленна (бизнес-ключ, название, цена, ссылка) → DropItem.
REQUIRED_FIELDS = ["sku", "name", "price_raw", "product_url"]
# Обязательные поля варианта 5: их пустота считается и контролируется smoke-check (порог 5 %).
MONITORED_FIELDS = ["name", "price_raw", "sku", "categories", "description", "image_url"]

SQLITE_PATH = str(DATA_DIR / "crawl.db")
SQLITE_BATCH_SIZE = 50                 # одна транзакция на 50 строк
STATS_DIR = str(DATA_DIR)              # сюда closed() пишет stats_<номер>.json

# Выгрузка Feed Exports: записи, прошедшие весь пайплайн, по одной JSON-строке.
FEEDS = {
    str(DATA_DIR / "items.jsonl"): {"format": "jsonlines", "encoding": "utf8", "overwrite": True},
}
FEED_EXPORT_ENCODING = "utf-8"

# Значения по умолчанию актуальных версий Scrapy, зафиксированные явно.
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
