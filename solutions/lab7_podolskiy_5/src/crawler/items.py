"""Схема записи: один товар учебного магазина (вариант 5).

Паук заполняет «сырые» поля (*_raw) и контекст первого уровня (list_*),
пайплайн (pipelines.py) проверяет, типизирует и сохраняет запись.
"""
import scrapy


class ProductItem(scrapy.Item):
    # --- бизнес-ключ и обязательные поля варианта 5 --------------------------------
    sku = scrapy.Field()                # артикул (SKU) — бизнес-ключ дедупликации
    name = scrapy.Field()               # название товара (h1.product_title)
    price_raw = scrapy.Field()          # текст цены с карточки: "$69.00", "$45.00 – $52.00"
    regular_price_raw = scrapy.Field()  # текст зачёркнутой цены (если товар со скидкой)
    categories = scrapy.Field()         # список категорий (span.posted_in a[rel=tag])
    description = scrapy.Field()        # полное описание (вкладка #tab-description)
    short_description = scrapy.Field()  # краткое описание (если есть)
    image_url = scrapy.Field()          # ссылка на основное изображение (полный размер)

    # --- поля, которые вычисляет пайплайн (звено 200) -------------------------------
    price = scrapy.Field()              # Decimal: текущая цена (для диапазона — минимальная)
    price_max = scrapy.Field()          # Decimal | None: верх диапазона у вариативного товара
    regular_price = scrapy.Field()      # Decimal | None: цена до скидки
    on_sale = scrapy.Field()            # bool: товар со скидкой
    currency = scrapy.Field()           # ISO-код валюты (USD, GBP, EUR)
    tags = scrapy.Field()               # метки (span.tagged_as) — не обязательны

    # --- служебные поля и контекст первого уровня (cb_kwargs) -----------------------
    product_url = scrapy.Field()        # URL карточки (после редиректов)
    product_id = scrapy.Field()         # внутренний id WooCommerce (data-product_id)
    sku_source = scrapy.Field()         # откуда взят артикул: html / jsonld / list
    list_page = scrapy.Field()          # URL страницы каталога, где найдена карточка
    list_page_no = scrapy.Field()       # номер страницы каталога
    list_position = scrapy.Field()      # позиция карточки на странице (1..16)
    list_title = scrapy.Field()         # название в списке (для сверки с карточкой)
    list_price_raw = scrapy.Field()     # цена в списке (для сверки с карточкой)
    run_id = scrapy.Field()             # идентификатор прогона
    scraped_at = scrapy.Field()         # время извлечения, ISO 8601 UTC
