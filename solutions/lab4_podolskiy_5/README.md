# Лабораторная 4 — динамические страницы: Playwright, дешёвая альтернатива и n8n (вариант 5)

**Студент:** Подольский Ф. А., ПМИ-3-2025 · **Вариант 5** (номер mod 6 + 1)

**Источник:** https://quotes.toscrape.com — учебная площадка Zyte для тренировки парсинга.
Основной источник — форма входа `/login` и разделы за входом; проверяемый механизм — **форма с CSRF,
`storage_state`, пагинация после авторизации**; контрольный статический источник — `/page/1`.
Сайт принимает любой логин/пароль (учебные креды `student`/`student`), реальные аккаунты не используются.

**Что собирается.** На сайте 100 цитат (10 страниц ленты). Объём ≥ 200 набирается честно: после входа
обходятся лента `/page/N/` и **разделы всех тегов** `/tag/<тег>/page/N/`. Запись итогового набора —
«цитата в разделе тега», бизнес-ключ `record_key = sha1(тег | quote_id)`, где
`quote_id = sha1(нормализованный текст | автор)` (у цитат нет id и URL). Повторное появление той же цитаты
в том же теге — дубль и отбрасывается; в разных тегах — разные факты принадлежности. Число уникальных цитат
и тегов выводится отдельно (`metrics/summary.md`, раздел 2). Поле **`goodreads_url`** («(Goodreads page)»)
сервер показывает **только после входа** — оно обязательное и служит контролем активной сессии.

> Разметка реального сайта в этой среде недоступна: код отлажен на локальной имитации
> (`tests/mock_site.py`) по известной разметке сайта. Селекторы сделаны с фолбэками;
> **первый реальный прогон — подтверждение разметки** (см. «Что проверить первым прогоном»).

## Файлы

```
lab4_podolskiy_5/
├── run_real.sh            # реальный прогон целиком (venv, Chromium, все шаги, лог в logs/)
├── collect.py             # браузерный сбор: Playwright (sync API), вход + storage_state + пагинация
├── light.py               # дешёвая альтернатива: requests.Session, CSRF-вход без браузера
├── common.py              # общее: конфиг, robots.txt, извлечение, ключи, цикл с предохранителями, журнал, smoke-check
├── diagnose.py            # протокол диагностики источника → metrics/diagnostics.json
├── compare.py             # сверка наборов браузер ↔ requests (+ no-code) → metrics/compare.json
├── bench.py               # замеры: блокировка ресурсов, eval_on_selector_all, storage_state против входа
├── faults.py              # бонус: подмена ответов route.fulfill (пустой / 500 / «истёкшая сессия») + trace
├── parallel.py            # бонус: async_playwright, K контекстов, ускорение и совпадение наборов
├── summarize.py           # таблицы для отчёта → metrics/summary.md
├── lab4.ipynb             # блокнот анализа (async API), сохранён без выводов
├── data_browser.csv       # набор браузера (цитата × тег)          ← после прогона
├── data_light.csv         # набор дешёвой альтернативы              ← после прогона
├── quotes_browser.csv / quotes_light.csv   # лента: 1 строка = цитата (для сверки и no-code)
├── collection_log.csv     # журнал итераций обоих подходов         ← после прогона
├── auth_state.json        # storage_state Playwright (учебные креды!) ← после прогона
├── auth_state_light.json  # cookies requests.Session (учебные креды!) ← после прогона
├── artifacts/             # png+html при сбоях, trace.zip, trace_faults.zip
├── metrics/               # *.json, bench_*.csv, summary.md
├── logs/                  # real_run_*.log
├── nocode/
│   ├── workflow.json      # экспорт n8n (вход с CSRF + пагинация + CSV)
│   ├── INSTRUCTIONS.md    # импорт, запуск, скриншоты, замеры; запасной вариант — Octoparse
│   ├── screenshot_*.png   # ← снять после прогона n8n
│   └── nocode_result.csv  # ← результат n8n
├── tests/
│   ├── mock_site.py       # локальная имитация quotes.toscrape.com (только для проверки)
│   └── run_mock_e2e.sh    # сквозная проверка всех шагов на имитации
├── report.md              # отчёт (⟪ЗАПОЛНИТЬ⟫ осталось только у времени разработки)
├── requirements.txt
└── .env.example
```

`auth_state.json` и `auth_state_light.json` намеренно **не** в `.gitignore`: это сессия учебного
пользователя на тренировочном сайте, их требует состав сдачи. Настоящие учётные данные туда класть нельзя.

## Запуск (macOS)

Всё сразу (≈ 15–25 мин — оценка, не замер; основное время — паузы вежливости 1 с между страницами
(10 страниц ленты + страницы всех тегов) в каждом подходе и повторы замеров):

```bash
cd lab4_podolskiy_5
bash run_real.sh          # лог: logs/real_run_<время>.log
```

По шагам (если нужно повторить отдельный этап):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium     # ~120 МБ (по данным занятия 4), один раз
cp .env.example .env

python diagnose.py                 # 1. протокол диагностики
python collect.py --trace          # 2–3. браузер: вход (1 раз) → storage_state → лента → теги
python collect.py --auth-check     #     новый контекст из auth_state.json, без входа
python light.py                    # 4. requests.Session + CSRF
python compare.py                  #     сверка наборов
python bench.py --reps 3           # 5. замеры оптимизаций
python faults.py                   # бонус: route.fulfill + trace_faults.zip
python parallel.py --workers 3     # бонус: параллельные контексты
python summarize.py                # таблицы → metrics/summary.md
jupyter notebook lab4.ipynb        # анализ (или исполняется в run_real.sh)
```

Пробный короткий прогон: `python collect.py --max-tags 5 --min-records 10`.
Показать окно браузера: `HEADLESS=0 python collect.py --max-tags 2 --min-records 1`.
Открыть трассу: `python -m playwright show-trace artifacts/trace_faults.zip`.

### Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `QTS_USERNAME`, `QTS_PASSWORD` | `student` / `student` | учебные креды (в выводе маскируются) |
| `POLITE_PAUSE` | `1.0` | пауза между запросами, с (с учётом `Crawl-delay`, если появится) |
| `HEADLESS` | `1` | `0` — показать окно браузера |
| `BASE_URL` | `https://quotes.toscrape.com` | только для теста на имитации |
| `OUT_DIR` | папка лабораторной | куда писать данные/метрики (тест пишет во временную папку) |
| `PW_CHROMIUM_PATH` | — | только тест: явный путь к Chromium |
| `MAX_ITERATIONS`, `MAX_PAGES_PER_SECTION`, `STALL_LIMIT`, `MAX_RECORDS`, `MAX_FAILURES`, `MAX_RUNTIME_S` | 600 / 30 / 2 / 5000 / 10 / 1800 | предохранители |

### Коды возврата

| Код | Значение |
|---|---|
| 0 | успех |
| 2 | вход не выполнен или сессия потеряна после одной повторной попытки входа |
| 3 | записей меньше `--min-records` (200) |
| 4 | smoke-check не пройден (только с `--strict`) |
| 5 | превышен лимит сбоев |
| 6 | `compare.py`: наборы расходятся |
| 7 / 8 | `faults.py` / `parallel.py`: проверка не пройдена |

### Проверка без интернета (локальная имитация)

```bash
bash tests/run_mock_e2e.sh /tmp/lab4_mock     # сервер-имитация на 127.0.0.1:8765, все шаги + блокнот
```

Имитация повторяет разметку сайта (карточка `div.quote[itemscope]`, `span.text[itemprop=text]`,
`small.author[itemprop=author]`, `a[href^="/author/"]`, `meta.keywords` + `a.tag`, `nav > ul.pager > li.next > a`,
форма входа с hidden `csrf_token`, после входа — «Logout» и «(Goodreads page)», `robots.txt` → 404), данные — синтетические.
Цифры с имитации в отчёт не переносятся.

## Локаторы и правила извлечения

| Поле / действие | Локатор (основной → фолбэк) | Приоритет занятия | Почему устойчив |
|---|---|---|---|
| поле логина | `get_by_label("Username")` → `input[name="username"]` | 3 → 4 | видимая метка; `name` — контракт с сервером |
| поле пароля | `get_by_label("Password")` + проверка `type=password` → `input[type="password"]` | 3 → 4 | на сайте метка «Password» может ссылаться на `for="username"`; тип поля не зависит от вёрстки |
| кнопка входа | `get_by_role("button", name="Login")` | 2 | роль + доступное имя |
| CSRF-токен | `input[name="csrf_token"]` (hidden) | 4 | имя поля обязано совпадать с серверной валидацией |
| признак входа | `get_by_role("link", name="Logout")` | 2 | роль + текст |
| карточка цитаты | `[itemtype*="schema.org/CreativeWork"]`, `div.quote` | 4 → 5 | микроразметка schema.org |
| текст / автор | `[itemprop="text"]` / `[itemprop="author"]` (→ `.text` / `.author`) | 4 | микроразметка |
| теги | `meta[itemprop="keywords"]@content` → тексты `a.tag` | 4 → 5 | данные, а не оформление |
| ссылка автора | `a[href*="/author/"]` без «goodreads» | 4 | шаблон URL |
| Goodreads | `a[href*="goodreads.com"]` → ссылка с текстом «Goodreads» | 4 → 3 | домен назначения |
| «Next» | `get_by_role("navigation").get_by_role("link", name=/^Next/)` → `li.next > a` | 2 → 5 | роль + имя в пределах навигации |

Структурный XPath и сгенерированные классы не используются. Извлечение — одним вызовом
`eval_on_selector_all(...)` через `textContent` (не зависит от CSS, поэтому блокировка стилей не меняет данные).

## Ожидания (без `time.sleep` для данных)

| Где | Чем ждём |
|---|---|
| форма входа | `expect(get_by_role("button", name="Login")).to_be_visible()` |
| отправка формы | `page.expect_response(POST /login)` + `expect(Logout).to_be_visible()` |
| переход «Next» | `page.expect_response(навигация на целевой URL)` + `page.wait_for_url(...)` |
| карточки на странице | `page.wait_for_selector(карточка, state="attached")`, таймаут 10 с → `TimeoutError` → артефакты |
| блокнот, бонус | `await page.wait_for_selector(...)` (async API) |

`time.sleep`/`asyncio.sleep` — только пауза вежливости между запросами (и повторы с backoff в requests).

## Соответствие требованиям

| Требование задания | Как реализовано |
|---|---|
| 4.1 протокол диагностики | `diagnose.py` → `metrics/diagnostics.json`, таблица — report.md §2 и блокнот §1 |
| ≥ 200 записей | итоговый набор «цитата × тег»; проверка `--min-records 200` (код 3) — в прогоне 16.10.2026 собрано 232 записи, проверка пройдена |
| ≥ 5 циклов взаимодействия | 9 кликов «Next» в ленте + страницы всех тегов; каждая итерация — строка в `collection_log.csv` |
| browser → context → page, закрытие | `collect.py`: `with sync_playwright()` + `try/finally` для `browser.close()` и `ctx.close()` |
| user_agent, viewport, locale, таймауты | `new_context(...)`: UA курса, 1280×900, `en-US`, `set_default_timeout(15 с)`, `set_default_navigation_timeout(30 с)` |
| ожидания без sleep | таблица «Ожидания» выше |
| локаторы по роли/тексту/метке/атрибутам, таблица | таблица «Локаторы»; report.md §4 — устойчивость к редизайну |
| три предохранителя | `common.Crawler`: максимум итераций (на запуск и на раздел), остановка после `STALL_LIMIT` страниц без прироста, лимит записей; плюс лимит сбоев, времени, одна попытка повторного входа |
| массовое извлечение + замер | `eval_on_selector_all`; `bench.py` B — сравнение с поэлементным перебором |
| блокировка медиа + замер | `context.route` → `abort()` для image/media/font/stylesheet; `bench.py` A |
| TimeoutError → скриншот + HTML, продолжение | `BrowserBackend` → `artifacts/*.png|html`, раздел пропускается после 1 повтора, обход идёт дальше; проверено `faults.py` |
| журнал CSV | `collection_log.csv`: run_id, approach, stage, section, iteration, page_no, url, n_elements, new_records, total_records, duration_s, requests, bytes, status, ts |
| проверка качества | `common.quality_report` / `typed`: число записей, доля пустых, дубликаты по `record_key`, типы (`Int64`, `boolean`, `datetime64[UTC]`) |
| нет повторного входа при каждом запуске | `auth_state.json` (storage_state) → вход только если сессия недействительна; `collect.py --auth-check` |
| 4.3 дешёвая альтернатива + сверка | `light.py` (requests.Session, CSRF) → `data_light.csv`; `compare.py` — ключи и поля, расхождения в `metrics/compare.json` |
| 4.4 no-code | `nocode/workflow.json` (n8n, проверен импортом и прогоном на имитации), `nocode/INSTRUCTIONS.md`; Octoparse — запасной вариант |
| 4.5 таблица сравнения | `summarize.py` → `metrics/summary.md` §4 → report.md §6 |
| 4.6 устойчивость + smoke-check | report.md §4; `common.smoke_check` (порог 5 %), вызывается в конце `collect.py`/`light.py`, демонстрация срабатывания — блокнот §4 |
| отчёт с правовым разделом | `report.md` §11 (правовая оценка), §10 (выводы по трём сценариям), §12 (источники) |
| бонус: параллельный сбор | `parallel.py` (async, K контекстов, совпадение наборов, ускорение) |
| бонус: Trace Viewer | `artifacts/trace.zip` (вход + лента), `artifacts/trace_faults.zip` (инцидент HTTP 500), разбор — report.md §9 |
| бонус: подмена ответа | `faults.py`: пустой ответ, 500, страница без сессии — PASS/FAIL |
| User-Agent, robots.txt, паузы | UA `PSU-edu-crawler/1.0 (...)`; `RobotsGuard` (robots.txt проверяется перед каждой страницей; на сайте 404); пауза 1 с |

## Что проверить первым реальным прогоном

1. `metrics/diagnostics.json → login_form.labels`: ссылается ли метка «Password» на `for="username"`
   (в `metrics/browser.json → auth.password_locator` видно, сработал ли фолбэк).
2. Есть ли «(Goodreads page)» на страницах **тегов** после входа (на ленте — точно; доля пустых `goodreads_url`
   в smoke-check должна быть 0 %).
3. Ответ на `POST /login` — редирект 3xx (на нём построена проверка в n8n).
4. Ссылка «Next» внутри `<nav>` (`get_by_role("navigation")`); если нет — сработает фолбэк `li.next > a`.
5. `compare.py`: наборы браузера и requests совпадают; `consistency_*`: число пар по метатегам ленты = числу пар
   на страницах тегов.
