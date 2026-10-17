# Лабораторная 5 — промышленный краулинг: Scrapy-проект и оркестрация в n8n

**Вариант 5.** Источник: учебный магазин `scrapingcourse.com/ecommerce` (WooCommerce, тема Storefront);
он закрыт robots.txt, поэтому итоговый прогон 16.10.2026 выполнен на согласованной с преподавателем замене
`scrapeme.live/shop` (400 записей, 26 страниц каталога).
Уровень 1 — пагинация каталога (`/ecommerce/`, `/ecommerce/page/N/`), уровень 2 — карточка товара:
название, цена, артикул (SKU), категории, описание, изображение.

Студент: Подольский Ф. А., ПМИ-3-2025.

## ⚠️ Источник варианта закрыт robots.txt — замена согласована

При подготовке (15.10.2026) файл `https://www.scrapingcourse.com/robots.txt` содержал ровно две строки:

```
User-agent: *
Disallow: /ecommerce/*
```

Правило запрещает **весь** каталог и все карточки (`/ecommerce/`, `/ecommerce/page/N/`, `/ecommerce/product/...`)
для любого User-Agent. Задание требует `ROBOTSTXT_OBEY = True` (иначе −15 баллов), поэтому
паук корректно отказывается собирать: делает один запрос (`robots.txt`) и отсекает стартовую
страницу (`robotstxt/forbidden = 1`, записей 0). Проверка в `run_real.sh` выполняется первой и
сохраняет доказательство в `data/robots_preflight.json` и `data/stats_*_robots_evidence.json`.

Второе ограничение: по описаниям площадки каталог занимает 12 страниц по 16 товаров, то есть
не более 192 уникальных SKU — требование «≥ 300 записей» на этом источнике невыполнимо даже
без запрета (обход по категориям даёт те же товары, дедупликация по SKU их схлопнет).
Точное число товаров проверить нельзя: страницы каталога закрыты тем же правилом.

**Замена источника.** Задание разрешает замену источника по согласованию с преподавателем. Проект
написан под разметку WooCommerce и без изменений кода работает на родственной учебной
площадке того же автора (ZenRows): **`https://scrapeme.live/shop/`** — 755 товаров на 48
страницах, `robots.txt` разрешает `/shop/` (запрещены только служебные пути и `?add-to-cart=`),
на карточке есть все поля варианта (название, цена, SKU, категории, описание, изображение).
Площадки нет в списке курса, поэтому замена согласована с преподавателем (текст запроса — ниже); итоговый прогон:

```bash
START_URL=https://scrapeme.live/shop/ ./run_real.sh
```

Текст запроса на согласование:

> Здравствуйте! Вариант 5 лабораторной № 5 — scrapingcourse.com/ecommerce. Сейчас его robots.txt
> запрещает весь каталог (`User-agent: *` / `Disallow: /ecommerce/*`, проверено 15.10.2026; лог
> проверки прилагаю), а при обязательном `ROBOTSTXT_OBEY = True` паук не может собрать ни одной
> записи. Кроме того, в каталоге не более 192 товаров (12 страниц), что меньше требуемых 300.
> Прошу согласовать замену на учебную площадку того же автора — scrapeme.live/shop (WooCommerce,
> 755 товаров на 48 страницах; robots.txt разрешает /shop/; персональных данных нет; на карточке
> те же поля: название, цена, SKU, категории, описание, изображение). Код проекта не меняется,
> меняется только стартовый URL. Подольский Ф. А., ПМИ-3-2025.

## Структура

```
lab5_podolskiy_5/
├── scrapy.cfg
├── ecommerce_crawler/              # проект: scrapy startproject ecommerce_crawler
│   ├── items.py                    # схема записи ProductItem
│   ├── pipelines.py                # 4 звена: 100 валидация, 200 очистка, 300 дедупликация, 400 SQLite
│   ├── settings.py                 # настройки с комментариями
│   └── spiders/ecommerce.py        # паук: каталог → карточка
├── data/                           # появляется после прогона
│   ├── crawl.db                    # SQLite: products + product_categories
│   ├── items.jsonl                 # Feed Exports итогового прогона
│   ├── stats_<N>_<метка>.json      # статистика КАЖДОГО прогона (closed())
│   ├── robots_preflight.json       # результат проверки robots.txt
│   └── demo/                       # демонстрации JOBDIR/HTTPCACHE, подмножество для n8n
├── jobstate/                       # каталог состояния JOBDIR (демонстрация)
├── quality/
│   ├── quality_report.ipynb        # полнота, причины отброса, коды, агрегаты, графики
│   └── smoke_check.py              # деградация между прогонами (−20 % записей / >5 % пустых)
├── nocode/
│   ├── workflow.json               # экспорт n8n
│   ├── INSTRUCTIONS.md             # импорт, запуск, скриншоты, результат реального запуска
│   ├── n8n_metrics.example.json    # шаблон замеров n8n
│   ├── n8n_metrics.json            # замеры реального запуска n8n (16.10.2026)
│   ├── n8n_errors.log              # журнал ветки ошибок n8n
│   ├── comparison_table.md         # таблица Scrapy vs n8n (tools/compare_table.py)
│   ├── screenshot_*.png            # скриншоты n8n: схема, выполнение, список, настройки, ветка ошибок, CSV
│   └── nocode_result.csv           # результат выполнения workflow (32 товара)
├── tools/
│   ├── robots_preflight.py         # robots.txt: Protego (как Scrapy) + urllib.robotparser
│   ├── demo_resume_cache.py        # п. 4.5: JOBDIR и HTTPCACHE с цифрами
│   ├── compare_table.py            # п. 4.8: таблица Scrapy vs n8n из измерений
│   └── mock_shop.py                # локальная имитация магазина (только для тестов)
├── report.md
├── requirements.txt
├── run_real.sh                     # всё для реального прогона, по порядку
└── README.md
```

`middlewares.py` из шаблона `startproject` удалён: собственные middleware в работе не нужны
(используются штатные: robots.txt, AutoThrottle, Retry, HttpCache, сжатие).

## Запуск на macOS

Нужен Python ≥ 3.10 (проверено на 3.11 и 3.13). Секретов нет, `.env` не нужен.

```bash
cd lab5_podolskiy_5
chmod +x run_real.sh
./run_real.sh                                          # источник варианта; при запрете robots.txt — стоп с доказательством
START_URL=https://scrapeme.live/shop/ ./run_real.sh    # согласованная замена (итоговый прогон 16.10.2026)
```

Скрипт сам создаёт `.venv`, ставит зависимости и пишет весь вывод в `logs/real_run_<время>.log`.

| Шаг | Что делает | Ожидаемое время |
|---|---|---|
| 1 | venv + `pip install -r requirements.txt` | 1–2 мин (первый раз) |
| 2 | `tools/robots_preflight.py` — решение «можно/нельзя» до сбора | 2 с |
| 3 | проверка селекторов на 1 странице (с HTTP-кэшем) | 15 с |
| 4 | итоговый прогон `run_label=main`, `CLOSESPIDER_ITEMCOUNT=400` | 5–7 мин (≈ 420 запросов при ~1,5 запр/с) |
| 5 | `tools/demo_resume_cache.py`: JOBDIR (4 стр.) и HTTPCACHE (3 стр.) | 2 мин |
| 6 | подмножество 2 стр. для сравнения с n8n | 30 с |
| 7 | smoke-check + self-test | 2 с |
| 8 | исполнение `quality/quality_report.ipynb` | 20 с |
| 9 | `tools/compare_table.py` (n8n-колонка — из `nocode/n8n_metrics.json`) | 1 с |

Итого ≈ 10–12 минут. Если источник закрыт robots.txt (вариант по умолчанию) — ≈ 1 минута:
скрипт фиксирует запрет и останавливается **с кодом 0**, чтобы не прерывать общий `run_all.sh`.

Ручные команды (из корня лабораторной, в активированном `.venv`):

```bash
scrapy crawl ecommerce -a start_url=https://scrapeme.live/shop/ -a run_label=main     # итоговый прогон
scrapy crawl ecommerce -a max_pages=2 -a run_label=test -s CLOSESPIDER_ITEMCOUNT=20   # быстрый тест
scrapy crawl ecommerce -s HTTPCACHE_ENABLED=True -a max_pages=1                       # отладка без нагрузки на сайт
scrapy shell "https://scrapeme.live/shop/"                                            # отладка селекторов
python tools/demo_resume_cache.py --start-url https://scrapeme.live/shop/
python quality/smoke_check.py          # сравнить два последних прогона с меткой main
python quality/smoke_check.py --self-test
```

Аргументы паука: `start_url` (по умолчанию источник варианта; можно и переменной `START_URL`),
`max_pages` (ограничить число страниц каталога), `run_label` (метка в имени `stats_<N>_<метка>.json`).

## Карта источника

| Уровень | Точка входа | Что извлекается | Переход дальше |
|---|---|---|---|
| 1. Каталог | `/ecommerce/` (или `/shop/`) | ссылки на карточки + контекст (название и цена в списке, позиция, `data-product_sku`, `data-product_id`) | `nav.woocommerce-pagination a.next` → `/page/N/` |
| 2. Карточка | `/ecommerce/product/<slug>/` | название, цена (со скидкой/диапазоном), SKU, категории, метки, краткое и полное описание, изображение | — |

Ключ уникальности — **SKU** (артикул), а не URL: один товар может быть доступен по двум адресам.

## Селекторы

Только семантические классы WooCommerce, `data-*`, `itemprop`, `rel`; индексных XPath нет. Для каждого
поля — цепочка фолбэков; последний рубеж — JSON-LD `Product`, который WooCommerce выводит на карточке.

| Поле | Основной селектор | Фолбэки | Как извлекается значение |
|---|---|---|---|
| карточка в списке | `ul.products li.product` | `li.type-product`, `li[class*='product_cat-']` | одна `li` = один товар, 16 на странице |
| ссылка на карточку | `a.woocommerce-LoopProduct-link::attr(href)` | `a.woocommerce-loop-product__link`, `a[href*='/product/']` | `response.follow` |
| SKU в списке | `a.add_to_cart_button::attr(data-product_sku)` | `[data-product_sku]` | контекст через `cb_kwargs`, фолбэк артикула |
| следующая страница | `nav.woocommerce-pagination a.next::attr(href)` | `a.next.page-numbers`, `link[rel='next']` | `response.follow` (Spider, не CrawlSpider) |
| название | `h1.product_title::text` | `div.summary h1`, `h1.entry-title`, `[itemprop='name']`, JSON-LD `name` | пробелы и `&nbsp;` схлопываются |
| цена | `div.summary p.price` → `.woocommerce-Price-amount` | `p.price`, `[itemprop='offers']`, JSON-LD `offers.price` | при скидке берётся `ins`, старая цена — `del`; диапазон «$45 – $52» → `price` = мин., `price_max` = макс.; → `Decimal` |
| SKU | `div.product_meta span.sku::text` | `[itemprop='sku']`, JSON-LD `sku`, `data-product_sku` из списка | `N/A` = пусто; источник пишется в `sku_source` |
| категории | `div.product_meta span.posted_in a[rel='tag']::text` | `span.posted_in a` | список → `product_categories` |
| описание | `div#tab-description` (текст без заголовка `h2`) | `div.woocommerce-Tabs-panel--description`, JSON-LD `description` | пробелы схлопываются |
| изображение | `div.woocommerce-product-gallery__image a::attr(href)` | `img::attr(data-large_image)`, `img::attr(src)`, `img.wp-post-image`, `og:image` | абсолютный URL; заглушка `woocommerce-placeholder` = пусто |

Разметка взята по шаблонам WooCommerce/Storefront и описаниям площадки (статьи ZenRows о scrapingcourse.com:
`li.product`, `h2.woocommerce-loop-product__title`, `a.page-numbers`, URL `/ecommerce/page/N/`).
Каталог scrapingcourse закрыт robots.txt, поэтому селекторы проверены на родственной площадке scrapeme.live: шаг 3
`run_real.sh` проверяет их на одной странице и печатает заполненность каждого поля. В прогоне 16.10.2026 все шесть
полей варианта заполнены у 100 % записей (`report.md`, раздел 8); порядок действий при поломке разметки — в разделе
«Если селекторы не сработали».

## Item Pipeline

| № | Класс | Что делает | Счётчики в статистике |
|---|---|---|---|
| 100 | `ValidationPipeline` | обязательные `sku`, `name`, `price_raw`, `product_url`; считает пустые значения всех 6 полей варианта | `quality/items_seen`, `quality/empty/<поле>`, `dropped/missing_fields`, `dropped/missing_fields/<поле>` |
| 200 | `CleanPipeline` | цена → `Decimal` (2 знака), диапазон и скидка, валюта → ISO, SKU → верхний регистр, текст без лишних пробелов, `product_id` → int, дата → ISO 8601 UTC; нечисловая цена → `DropItem` | `dropped/bad_price`, `clean/placeholder_image`, `clean/title_mismatch_list_vs_card`, `clean/currency_assumed` |
| 300 | `DedupPipeline` | дубли по SKU | `dropped/duplicate_sku` |
| 400 | `SQLitePipeline` | соединение в `open_spider`, пакеты по 50 строк `executemany`, `INSERT … ON CONFLICT(sku) DO UPDATE` (UPSERT), закрытие в `close_spider` | `db/rows_written`, `db/batches`, `db/rows_total_in_table` |

Деньги хранятся точно: `price TEXT` (`'69.00'`, из `Decimal`) + `price_cents INTEGER` для арифметики —
так задание «цена → число» выполняется без ошибок округления `float`.

## Проверка на локальной имитации

Помимо реального прогона, сценарий целиком прогнан end-to-end на имитации
`tools/mock_shop.py`: те же классы WooCommerce, 331 синтетический товар на 21 странице и
специально подготовленные «трудные» карточки (нет SKU, «SKU: N/A», SKU только в JSON-LD,
дубль SKU под другим URL, «Price on request», нет описания, заглушка вместо фото, грязный заголовок).

```bash
python3 tools/mock_shop.py --port 18505 &                     # имитация (robots.txt разрешающий)
START_URL=http://127.0.0.1:18505/ecommerce/ ./run_real.sh     # тот же сценарий, что и на реальном сайте
python3 tools/mock_shop.py --port 18515 --robots real &       # копия настоящего robots.txt scrapingcourse
START_URL=http://127.0.0.1:18515/ecommerce/ ./run_real.sh     # → запрет, 1 запрос, 0 записей
```

Результаты (на локальной имитации): итоговый прогон — 353 запроса, 327 записей, отброшено 4
(2 без SKU, 1 нечисловая цена, 1 дубль SKU), 214 с при настройках из `settings.py`; SKU из JSON-LD
восстановлен у 1 карточки; JOBDIR — 30 + 31 запись, `dupefilter/filtered = 17`; HTTPCACHE — 52
ответа записаны, во втором прогоне 52 попадания и 0,74 с против 34 с; n8n (CLI n8n 2.43.6) — 30 записей
за 18,9 с. Это проверка работоспособности, а не результаты лабораторной.

## Соответствие требованиям

| Требование задания | Где и как выполнено |
|---|---|
| Проект `scrapy startproject` | `scrapy.cfg`, пакет `ecommerce_crawler/` |
| Схема записи | `items.py`: `ProductItem`, используется в пауке |
| Логика паука отдельно от обработки | паук только извлекает; вся обработка — `pipelines.py` |
| Начальные URL | `start()` / `start_requests()` (совместимость со Scrapy < 2.13), `start_url` из аргумента/переменной |
| Пагинация через `response.follow` | `parse()` → `nav.woocommerce-pagination a.next` |
| Двухуровневый обход + `cb_kwargs` | `parse` → `parse_item(list_ctx=…)`: страница, номер, позиция, название/цена/SKU из списка |
| Стабильные селекторы, без индексных XPath | таблица выше; единственный XPath — `.//text()[not(ancestor::h2)]` |
| Устойчивость к отсутствующим полям | `.get()` + фолбэки + JSON-LD; нетипичные карточки имитации не роняют паука |
| `errback` с записью в статистику | `on_error`: `errback/total`, `errback/http_<код>`, `errback/timeout`, `errback/dns`, `errback/ignored` + список в stats-файле |
| `closed()` → `stats_<N>.json` | `data/stats_<N>_<метка>.json`, номер прогона — автоматически |
| ≥ 300 записей, ≥ 10 страниц, ≥ 2 уровня | на согласованном источнике: `CLOSESPIDER_ITEMCOUNT=400`, 48 страниц; на scrapingcourse — невозможно (см. выше). Прогон 16.10.2026: 400 записей, 26 страниц каталога, 2 уровня |
| 4 звена пайплайна, `DropItem` со счётчиками | таблица «Item Pipeline» |
| Пакетная запись, UPSERT, соединение в `open_spider` | `SQLitePipeline` |
| Настройки с обоснованием | `settings.py` (комментарий к каждому значению), таблица в `report.md` |
| `ROBOTSTXT_OBEY=True`, AutoThrottle, ≤ 4 на домен, `DEPTH_LIMIT`, `CLOSESPIDER_*` | `settings.py`: True, `AUTOTHROTTLE_ENABLED`, `CONCURRENT_REQUESTS_PER_DOMAIN=2`, `DEPTH_LIMIT=60`, `CLOSESPIDER_ITEMCOUNT/PAGECOUNT/TIMEOUT/ERRORCOUNT` |
| Нет `time.sleep` в колбэках, нет соединения с БД в `process_item` | проверено: паузы — только `DOWNLOAD_DELAY`/AutoThrottle |
| JOBDIR: прерывание, состояние, `dupefilter/filtered` | `tools/demo_resume_cache.py` → `data/demo/demo_summary.md` |
| HTTPCACHE: два прогона, `store`/`hit`, время | там же |
| Отчёт о качестве | `quality/quality_report.ipynb` (полнота, причины отброса, коды, повторы, трафик, длительность, 3 агрегата, графики) |
| Smoke-check | `quality/smoke_check.py`: −20 % записей / > 5 % пустых обязательных полей, код возврата 1; `--self-test` |
| n8n-эквивалент ≥ 30 записей | `nocode/workflow.json` (2 стр. × 16): Schedule + Manual Trigger, HTTP Request (Retry On Fail 5×5 с, по одному запросу), HTML Extract, Split Out, Remove Duplicates ×2, Loop Over Items ×2, Wait, Code (типизация), Convert to File → CSV, ветка ошибки с журналом и Telegram |
| Таблица сравнения из измерений | `tools/compare_table.py` → `nocode/comparison_table.md`; время n8n — из журнала выполнений |
| Отчёт 3–5 стр. | `report.md` |

## Если селекторы не сработали

1. `scrapy shell "<URL страницы каталога>"`, затем `response.css("ul.products li.product")` — сколько карточек.
2. Если 0 — посмотреть `response.text[:2000]`: возможно, другая тема WooCommerce. Добавить новый
   селектор **в начало** соответствующего списка `SEL` в `spiders/ecommerce.py` (фолбэки оставить).
3. Повторить шаг 3 `run_real.sh` (он работает с HTTP-кэшем и не нагружает сайт повторно).

## Источники цифр в отчёте

Цифры итогового прогона, JOBDIR/HTTPCACHE, отчёта о качестве и n8n получены в прогонах 16.10.2026.
Источники: `logs/real_run_*.log`, `data/stats_*.json`, `data/demo/demo_summary.md`, исполненный
`quality/quality_report.ipynb`, `nocode/comparison_table.md`.
