# Лабораторная 7 — устойчивый и легальный пайплайн: скорость, повторы, наблюдаемость

**Вариант 5 — профиль «Scrapy-краулер».** Обязательные сценарии: **429 от домена**, **срабатывание
AutoThrottle**, **прерывание и возобновление прогона**.
Исходный сборщик — Scrapy-краулер лабораторной № 5 (каталог WooCommerce → карточка товара),
доработанный до устойчивого пайплайна. Студент: Подольский Ф. А., ПМИ-3-2025.

## ⚠️ robots.txt источника

`https://www.scrapingcourse.com/robots.txt` (проверено 15.10.2026) запрещает весь каталог:
`User-agent: *` / `Disallow: /ecommerce/*`. Сборщик это соблюдает: `src.collect` проверяет robots.txt до
сбора и завершается с кодом 3, Scrapy дополнительно держит `ROBOTSTXT_OBEY = True`. Прогоны A/B/C на реальном
сайте возможны только на согласованном с преподавателем источнике — предложен `https://scrapeme.live/shop/`
(тот же автор и та же разметка WooCommerce, robots.txt разрешает `/shop/`; черновик письма — в README
лабораторной № 5). После согласования:

```bash
START_URL=https://scrapeme.live/shop/ ./run_real.sh
```

Без согласования `run_real.sh` всё равно выполняет: проверку robots.txt (с доказательством запрета),
сценарии кодов ответа на httpbin.org и все сценарии отказов на локальной имитации.

Отдельная находка: `urllib.robotparser` в Python ≤ 3.12 (и в ранних 3.13.x) **не понимает** шаблон `*`
и на это правило отвечает «можно». Поэтому robots.txt читается через `urllib.robotparser` (как требует
задание) и перепроверяется Protego (RFC 9309, его же использует Scrapy); решение — по более строгому ответу.

## Структура

```
lab7_podolskiy_5/
├── src/
│   ├── limiter.py          # token bucket по доменам + джиттер, адаптивная задержка, Crawl-delay,
│   │                       #   robots.txt (urllib.robotparser + Protego), общий счётчик для N процессов
│   ├── retry.py            # классификация ошибок, full jitter + cap + попытки + бюджет, Retry-After
│   ├── breaker.py          # circuit breaker closed → open → half-open по доменам
│   ├── detector.py         # детектор блокировок (код + маркеры + размер + элементы), артефакты, история
│   ├── observability.py    # JSON-журнал, отчёт о прогоне, 8 порогов, итоговый статус
│   ├── collect.py          # доработанный сборщик: точка входа, режимы A/B/C, сценарии
│   └── crawler/            # Scrapy-часть (из лаб. 5): паук, пайплайн, middleware, расширение
│       ├── middlewares.py  #   ResilienceMiddleware, ConditionalRequestMiddleware, BlockDetectorMiddleware
│       ├── extensions.py   #   RunObserver: AutoThrottle в журнал, отчёт, пороги, история
│       ├── runtime.py      #   общее состояние прогона
│       ├── spiders/        #   ecommerce.py (каталог → карточка), probe.py (коды ответа)
│       ├── items.py, pipelines.py, settings.py
├── state/                  # etags.json, watermark.json, deferred.json — между прогонами
├── logs/
│   ├── run_<id>.jsonl      # структурный журнал каждого прогона
│   └── incidents/          # артефакты подозрительных ответов: body.html, headers.json, meta.json
├── reports/
│   ├── run_report_A.json / _B.json / _C.json   # и отчёты всех остальных прогонов
│   ├── comparison.md       # таблица A/B/C и экономия по механизмам
│   └── history.jsonl, *_summary.json, shared_limit.json, robots_report_*.json
├── analysis.ipynb          # графики, метрики, выводы
├── nocode/
│   ├── workflow.json, workflow_error_handler.json
│   ├── INSTRUCTIONS.md, nocode_notes.md
│   ├── nocode_result.csv   # результат первого запуска n8n (32 записи)
│   └── screenshot_*.png    # скриншоты n8n: схема, запуски, повторы, обработчик ошибок
├── tools/                  # mock_shop.py (имитация), run_mock_scenarios.py, resume_demo.py,
│                           #   demo_shared_limit.py, make_comparison.py
├── data/crawl.db           # собранные записи (появляется после прогона)
├── legal_checklist.md
├── report.md
├── requirements.txt
├── run_real.sh
└── README.md
```

## Запуск на macOS

```bash
cd lab7_podolskiy_5
chmod +x run_real.sh
./run_real.sh                                         # ≈ 10 мин без реального источника
START_URL=https://scrapeme.live/shop/ ./run_real.sh   # ≈ 14 мин с согласованным источником
```

Python ≥ 3.10. Секретов нет. Нужны свободные локальные порты 18507–18509 (имитация). Переменные:
`START_URL` (источник), `HTTPBIN` (стенд кодов, по умолчанию `https://httpbin.org`), `PYTHON`.

| Шаг `run_real.sh` | Время |
|---|---|
| venv и зависимости (первый раз) | 1–2 мин |
| robots.txt источника и httpbin | 5 с |
| A, B, C, S0–S3 на источнике (по ≈ 18 запросов; A ≤ 30) | ≈ 1,5 мин |
| прерывание/возобновление (2 стр.), предохранитель (второй домен httpbin) | ≈ 2 мин |
| коды ответа на httpbin (429, 401, 403, 404, ETag, gzip, 503, таймаут) | ≈ 2–3 мин |
| сценарии на локальной имитации (`tools/run_mock_scenarios.py`) | ≈ 7 мин |
| лимит 4 процессов, таблицы, блокнот | ≈ 1 мин |

Отдельные команды (из корня, в `.venv`):

```bash
python -m src.collect --robots-only --start-url https://www.scrapingcourse.com/ecommerce/
python -m src.collect --mode B --pages 1 --label B --start-url https://scrapeme.live/shop/ --fresh-state
python -m src.collect --mode C --pages 1 --label C --start-url https://scrapeme.live/shop/
python -m src.collect --label probe --probe-urls "https://httpbin.org/status/429|https://httpbin.org/etag/x"
python tools/resume_demo.py --start-url https://scrapeme.live/shop/ --pages 2 --prefix real_resume
python tools/run_mock_scenarios.py --only 429,autothrottle,resume
```

Коды возврата `src.collect`: 0 — статус зелёный/жёлтый, 1 — красный, 2 — детектор зафиксировал блокировку
(сбор домена остановлен, артефакты в `logs/incidents/`), 3 — robots.txt запрещает обход.

## Как устроено (кратко)

* **Скорость.** `ResilienceMiddleware` перед каждой сетевой попыткой берёт токен у ограничителя своего домена
  (`DomainLimiter`: домен → token bucket 2 запр/с, всплеск 2, джиттер 25 %) с учётом адаптивной задержки
  (латентность сервера; удвоение при 429/503), Crawl-delay из robots.txt (приоритет) и паузы после 429/503.
  Короткие ожидания — асинхронно (`deferLater`), длинные — запрос возвращается в очередь по таймеру и не держит
  слот загрузчика. AutoThrottle Scrapy включён как вторая обратная связь; его задержка пишется в журнал.
* **Повторы.** Встроенный RetryMiddleware выключен; `src/retry.py`: классы ошибок, full jitter
  `random(0, min(20, 0.5·2^n))`, 4 попытки, бюджет 90 с на URL, Retry-After (секунды и дата) важнее backoff,
  4xx не повторяются (кроме 408/429), 401 и 403 — отдельные события без повторов.
* **Предохранитель** на домен: 3 отказа подряд (5xx/сеть) → open 10 с → half-open (1 пробный) → closed;
  запросы к открытому домену не уходят в сеть, другие домены продолжают работать.
* **Нагрузка.** ETag/Last-Modified в `state/etags.json` → `If-None-Match`/`If-Modified-Since` → 304;
  gzip; «водяной знак» `state/watermark.json` (отпечаток карточки в каталоге + время проверки): неизменившиеся
  карточки не запрашиваются, каталог при 304 берёт ссылки из водяного знака.
* **Детектор.** Код + маркеры («captcha», «access denied», «доступ ограничен»…) + размер + наличие
  `li.product`/`h1.product_title`; «блокировка» → домен остановлен, артефакты, код возврата 2; сравнение
  с медианой прошлых прогонов (записи < 80 %, средний ответ < 70 % → красный).
* **Наблюдаемость.** Каждая строка `logs/run_<id>.jsonl` — JSON с полями `ts, run_id, event, host, url,
  status, ms, attempt, bytes, error`; отчёт `reports/run_report_<метка>.json` с 8 порогами и статусом.
* **Возобновление.** `--jobdir`: очередь и отпечатки Scrapy на диске + наше состояние (`state/`), URL,
  ждавшие по таймеру в момент остановки, — в `state/deferred.json` (`--resume-deferred`).

## Проверка на локальной имитации

Реальные площадки из среды разработки недоступны, поэтому каждый сценарий воспроизведён на `tools/mock_shop.py`
(WooCommerce-разметка, серверный лимит 429 с Retry-After в секундах/датой/без заголовка, авария 503,
капча с кодом 200, окно медленных ответов, ETag/304, gzip, эндпоинты как у httpbin). Все отчёты имитации
имеют метку `mock_*` и флаг `local_mock: true`. Итог прогона всех сценариев (на локальной имитации):

| Сценарий | Результат |
|---|---|
| A / B / C (1 стр. + 16 карточек, сервер ≤ 5 запр/с) | A: 1,15 с, 7 × 429, 9 записей; B: 8,14 с, 0 ошибок, 15 записей; C: 2 запроса вместо 18, трафик 0,1 КБ вместо 107,7 КБ |
| 429 (сервер 3 запр/с, мы намеренно 8 запр/с) | 3 × 429 из 38 запросов, 2 повтора по Retry-After, 1 по backoff; собраны все 30 записей за 24,5 с (при 2 запр/с — 0 × 429 и 16,6 с) |
| AutoThrottle (латентность +600 мс в окне) | задержка 14,7 → 620,7 мс и обратно до 14,7 мс |
| Прерывание SIGINT через 8 с и возобновление | 15 + 31 запись = все 46, `dupefilter/filtered = 17`, дублей по SKU нет |
| Авария второго домена (503 первые 8 с) | каталог закончен за 25 с без ошибок; второй домен: closed → open → half-open → closed |
| Заглушка-капча на стр. 2 | вердикт «блокировка», артефакты, код 2; записей 15 против медианы 30 → «красный» |
| Экономия | ETag/304: −99,8 % трафика; gzip: −70,9 %; водяной знак: −88,9 % запросов |
| 4 процесса, лимит 2 запр/с | локальные ведра: 8,85 запр/с на сервере; общий счётчик: 2,0 запр/с |

## Соответствие требованиям

| Пункт | Где выполнено |
|---|---|
| 4.1 token bucket, джиттер 20–30 %, лимиты по доменам | `src/limiter.py`: `TokenBucket`, `DomainPolicy` (jitter 0.25), `DomainLimiter` (домен → ограничитель) |
| 4.1 адаптивная задержка по латентности, рост при 429/503 | `AdaptiveDelay.observe`; вызывается в `ResilienceMiddleware.process_response`; + AutoThrottle |
| 4.1 robots.txt: can_fetch, crawl_delay, request_rate, Sitemap, 404 | `read_robots()`; Crawl-delay и Request-rate попадают в лимит домена (`collect.build_settings`); 404/401/403/5xx — `policy_note`; отчёт — блокнот, раздел 2 |
| 4.1 обоснование лимитов, 4 процесса | `report.md`, раздел 3; `SharedTokenBucket` + `tools/demo_shared_limit.py` |
| 4.2 классификация, full jitter, cap, попытки, бюджет | `src/retry.py` (`classify`, `full_jitter`, `RetryPolicy`) |
| 4.2 Retry-After (число и дата) в приоритете | `parse_retry_after`, `RetryPolicy.next_delay`; журнал: `delay_source = retry-after / backoff` |
| 4.2 запрет повторов 4xx, 401/403 отдельно | `classify`: 401 → `auth_required`, 403 → `forbidden_possible_block` (инцидент), прочие 4xx → без повторов |
| 4.2 журнал каждой попытки | события `request`, `retry` (attempt, status, delay_s, delay_source), `giveup` |
| 4.3 предохранитель, изоляция, метрики | `src/breaker.py`; `breaker_activations`, `breaker_rejected` в отчёте; сценарий `mock_breaker`, `real_breaker` |
| 4.4 условные запросы (304) | `ConditionalRequestMiddleware`, `state/etags.json`; httpbin `/etag/<tag>` и источник |
| 4.4 gzip, инкрементальность, таблица экономии | режим C / `--gzip`, `state/watermark.json`; `reports/comparison.md` |
| 4.5 детектор: код, маркеры, размер, элементы; вердикт | `src/detector.py::inspect`, `BlockDetectorMiddleware` |
| 4.5 реакция: стоп домена, артефакты, журнал, код ≠ 0 | `domain_blocked`, `logs/incidents/`, `incident`, код 2 |
| 4.5 сравнение с историей | `compare_history` (медиана прошлых прогонов), `reports/history.jsonl` |
| 4.6 JSON-логи с обязательными полями | `src/observability.py::JsonLogger`, `logs/run_<id>.jsonl` |
| 4.6 отчёт о прогоне, ≥ 5 порогов, статус | `build_run_report`, `THRESHOLDS` (8), `evaluate_alerts` |
| 4.6 два графика | `analysis.ipynb`, раздел 11 (латентность, коды по времени) + AutoThrottle и предохранитель |
| 4.7 прогоны A/B/C, A ≤ 30 запросов на учебной площадке | `run_real.sh` (`--max-requests 30`), `reports/run_report_{A,B,C}.json`, `comparison.md` |
| 4.8 n8n: Wait, Retry, Error Trigger, IF, расписание | `nocode/workflow.json` + `workflow_error_handler.json`; таблица и стоимость — `nocode/nocode_notes.md` |
| 4.9 правовой чек-лист | `legal_checklist.md` (12 пунктов) |
| Сценарии варианта 5 | 429 — `mock_429`, `httpbin_codes`; AutoThrottle — `mock_autothrottle`, события `autothrottle` в B/C; прерывание — `tools/resume_demo.py` |
| Без обхода защит | нет прокси, ротации UA, решения капчи; UA честный с контактом; при блокировке — остановка |

## Что было заполнено после прогона

Цифры реального прогона 16.10.2026 внесены в `report.md`, `legal_checklist.md` и `nocode/nocode_notes.md`;
скриншоты n8n лежат в `nocode/`. Источники: `reports/comparison.md`, `reports/run_report_*.json`,
исполненный `analysis.ipynb`, `logs/real_run_*.log`.
