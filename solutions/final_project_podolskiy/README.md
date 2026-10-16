# Итоговый проект — гибридный пайплайн сбора данных

**Студент:** Подольский Ф. А., ПМИ-3-2025 · **Курс:** «Инструменты автоматизированного сбора данных в сети Интернет»
**Конфигурация:** B (гибридная) — код собирает и обрабатывает, n8n оркестрирует расписание, проверку отчёта и оповещения.

**Исследовательский вопрос:** *Как популярность и активность Python-репозиториев связаны с их тематикой и размером?*
(связка № 5 из списка разрешённых источников)

| Источник | Природа | Что даёт | Инструмент |
|---|---|---|---|
| github.com/topics/&lt;topic&gt;?l=python | HTML-сайт | тематическая принадлежность, позиция в выдаче, звёзды на карточке, дата «Updated» | `requests` + BeautifulSoup (браузер не нужен — см. диагностику) |
| api.github.com | REST API | звёзды, форки, наблюдатели, open issues, размер, даты создания/push, лицензия, темы, архивность | `requests`, условные запросы ETag, водяной знак |

Связь — по бизнес-ключу `owner/repo` (в нижнем регистре). Темы: `web-scraping`, `fastapi`, `machine-learning`,
`data-engineering`, `cli`, `telegram` — по 5 страниц × 20 карточек → 600 карточек, ≈ 560 уникальных репозиториев
(обоснование выбора тем — `docs/decisions.md`, раздел 1).

---

## 1. Воспроизведение с нуля (macOS)

```bash
cd final_project_podolskiy
cp .env.example .env          # вписать GITHUB_TOKEN (fine-grained токен без прав, только чтение публичных данных)
./run_real.sh                 # всё по порядку, журнал — logs/real_run_<время>.log
```

`run_real.sh` создаёт `.venv` (если нет), ставит зависимости и выполняет:

1. `pytest` (парсер на HTML-фикстурах, контракт, UPSERT, robots, политика повторов);
2. диагностику HTML-источника и robots.txt → `reports/diagnostics_latest.json`;
3. **прогон 1** — полный сбор (вставка);
4. сохраняет первую реальную страницу темы в `tests/fixtures/real_topic_p1.html` и повторяет тест парсера на ней;
5. **прогон 2** — сразу же: водяной знак, API почти не запрашивается (UPSERT: нули);
6. **прогон 3** — перепроверка всех репозиториев условными запросами (304 / обновления изменившихся);
7–9. демонстрационные сценарии: инкрементальный прогон с 304, отказ источника, обрыв и возобновление;
10. исполняет `analysis.ipynb` на реальных данных (выводы и графики сохраняются в блокноте и `mart/fig*.png`).

То же вручную:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                                  # тесты
python src/collect_html.py --diagnose                # нужен ли браузер + robots.txt
python src/run_pipeline.py                           # полный прогон по config.yaml
python src/run_pipeline.py --topics web-scraping --max-pages 1 --refresh-max-age-hours 0   # быстрый прогон с 304
jupyter notebook analysis.ipynb                      # или: Run All в VS Code
```

### Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `GITHUB_TOKEN` | токен GitHub API (5000 запр/ч; 304 не расходуют квоту). Только из окружения/`.env`, в логах — маска `ghp_…xy` | нет → режим `search` (60 запр/ч + Search API 10 запр/мин) |
| `GITHUB_HTML_BASE` | адрес HTML-источника (локальная имитация, демонстрация отказа) | `https://github.com` |
| `GITHUB_API_BASE` | адрес API | `https://api.github.com` |
| `PIPELINE_DATA_ROOT` | отдельный каталог данных (raw/clean/mart/db/logs/reports/state), например для имитации | корень проекта |
| `PYTHON` | интерпретатор для создания `.venv` в `run_real.sh` | `python3` |

### Ожидаемое время (оценка по лимитам `config.yaml`, не замер)

| Шаг | Запросов | Темп | Оценка |
|---|---|---|---|
| Прогон 1 (полный) | ≈ 30 HTML + ≈ 560 API | 0,33 запр/с к github.com; 1,5 запр/с к API | ≈ 8–10 мин |
| Прогон 2 (сразу же) | ≈ 30 HTML + новые ключи | — | ≈ 2 мин |
| Прогон 3 (ETag) | ≈ 30 HTML + ≈ 560 условных API | — | ≈ 8–10 мин |
| Демо-сценарии + блокнот | ≈ 60 | — | ≈ 2–3 мин |
| **Итого `run_real.sh`** | | | **≈ 20–25 мин** + установка пакетов |

Проверка `run_real.sh` целиком **на локальной имитации с теми же лимитами** (Python 3.11, `.venv` с нуля):
прогон 1 — 458 с, прогон 2 — 89 с, прогон 3 — 458 с, быстрый инкрементальный — 12 с, отказ API — 6 с,
весь скрипт — 18,7 мин. На реальном GitHub добавится сетевая задержка. Фактическая длительность каждого прогона —
в `reports/run_report_<id>.json` (поле `duration_s`). **Реальный прогон 15.10.2026** (Python 3.10, токен): прогон 1 — 484 с,
прогон 2 — 98 с, прогон 3 — 489 с, быстрый инкрементальный — 14 с, отказ API — 6 с, обрыв и продолжение — 66 с.

---

## 2. Структура проекта

```
final_project_podolskiy/
├── README.md                 # этот файл: воспроизведение с нуля, соответствие требованиям
├── slides.md                 # содержание 5–7 слайдов защиты (PDF собирается отдельно)
├── config.yaml               # источники, темы, лимиты, пороги качества и оповещений, расписание
├── requirements.txt  .env.example  run_real.sh
├── src/
│   ├── collect_html.py       # сбор HTML: страницы тем, парсер (3 стратегии), детектор блокировки, диагностика
│   ├── collect_api.py        # сбор API: водяной знак, ETag, режим search без токена, минимизация данных
│   ├── clean.py              # очистка с протоколом: raw -> clean (Parquet) + rejects.jsonl
│   ├── contract.py           # контракт данных (pydantic): TopicListing, RepoRecord, собственные валидаторы
│   ├── storage.py            # схема БД, UPSERT, снапшоты, отбраковка, журнал прогонов, слой mart
│   ├── quality.py            # 9 метрик качества, пороги, вердикт (красный -> mart не обновляется)
│   ├── observability.py      # JSON-логи, инциденты, отчёт о прогоне, 7 порогов оповещения
│   ├── run_pipeline.py       # единая точка входа (коды возврата, RUN_SUMMARY для n8n)
│   ├── http_client.py        # общий вежливый клиент: token bucket, повторы, Retry-After, breaker, ETag, robots RFC 9309
│   └── settings.py           # пути, config.yaml, .env, время в UTC, атомарная запись состояния
├── raw/                      # JSON Lines, только дозапись (+ raw/html_pages/*.html.gz, raw/robots/*.txt)
├── clean/                    # repos.parquet, topic_listings.parquet, rejects.jsonl, cleaning_protocol.json
├── mart/                     # topic_summary, size_band_topic, activity_band_topic, repo_topic, stars_by_day_topic (+ fig*.png)
├── db/schema.sql, warehouse.db
├── logs/run_*.jsonl, logs/incidents/*.json, logs/real_run_*.log
├── reports/run_report_*.json, quality_*.json, diagnostics_latest.json, latest_run_report.json
├── state/watermark.json, etags.json, checkpoint.json
├── nocode/workflow.json, notes.md, INSTRUCTIONS.md, screenshots/
├── analysis.ipynb            # по структуре шаблона итогового проекта: доказательства, метрики, выводы, графики
├── docs/pipeline_diagram.md, data_card.md, legal_checklist.md, decisions.md (обоснование решений по шести осям защиты)
├── tests/                    # pytest: парсер на HTML-фикстурах, robots, повторы, контракт, UPSERT
└── demo/                     # mock_github.py (имитация), demo_incremental.sh, demo_failure.sh, demo_resume.sh, run_mock_e2e.sh
```

`src/http_client.py` и `src/settings.py` — общие модули сверх перечня ТЗ: вежливый клиент используется обоими
сборщиками, поэтому вынесен отдельно, а не продублирован.

---

## 3. Как устроен прогон

Схема — `docs/pipeline_diagram.md`. Кратко:

1. **robots.txt** github.com → правила группы `*` (собственный матчер RFC 9309 + сравнение с `urllib.robotparser`); при
   недоступности robots.txt (5xx/сеть) HTML-источник пропускается (RFC 9309: считать сайт закрытым).
2. **HTML**: `/topics/<topic>?l=python&page=1..5` → карточки → `raw/github_topics_html.jsonl` + снимок страницы.
3. **API**: ключи из карточек → отбор по водяному знаку → `GET /repos/{owner}/{repo}` с `If-None-Match` → 200 пишется
   в `raw/github_api_repos.jsonl`, 304 — только отметка проверки.
4. **clean**: протокол очистки, дедупликация, контракт pydantic → Parquet + `rejects.jsonl`.
5. **БД**: UPSERT `repos`, `topic_listings`, синонимы ключей, снапшоты, отбраковка, журнал прогонов.
6. **качество**: 9 метрик → вердикт; **красный — mart не обновляется** (код 2).
7. **mart**: представление `v_repo_topic` (соединение двух источников) + агрегаты в Parquet/CSV.
8. **отчёт** из JSON-журнала, 7 порогов оповещения, `RUN_SUMMARY {...}` — последняя строка вывода для n8n.

### Коды возврата `src/run_pipeline.py`

| Код | Значение |
|---|---|
| 0 | прогон завершён, вердикт зелёный или жёлтый |
| 2 | красный статус качества — mart не обновлялся |
| 3 | источник недоступен/заблокирован: отказ изолирован, записан инцидент, прогон деградировал |
| 4 | сработал предохранитель `max_requests_per_run` (состояние сохранено) |
| 5 | данные загружены, но сработало красное оповещение (обвал объёма, 403/429, доля ошибок) |
| 130 | прогон прерван (Ctrl+C / `--simulate-crash-after`) — следующий запуск продолжит |
| 1 | непредвиденная ошибка (трассировка — в `logs/incidents/`) |

### Селекторы HTML-источника (страница темы)

Разметка воспроизведена по знанию вёрстки github.com/topics; содержание страницы (20 карточек, «Most stars», фильтр
`l=python`, звёзды вида «71.3k», «Updated <дата>», кнопка «Load more…») подтверждено чтением реальной страницы
15.10.2026, а **классы и атрибуты нужно подтвердить первым реальным прогоном** (шаг 4 `run_real.sh`).

| Поле | Селектор (внутри `article`) | Как получаем значение | Фолбэк |
|---|---|---|---|
| карточка | `article` | одна карточка = один репозиторий | JSON в `script[type=application/json]`, затем `h3 a[href]` |
| `owner/repo` | `h3 a[href]` | `href` вида `/owner/repo` (регулярное выражение; `/login`, `/topics/…` отсекаются) | любая `a[href]` карточки |
| звёзды | `#repo-stars-counter-star`, `span.js-social-count` | точное число из `aria-label` («N users starred…») или `title` («71,321»); иначе текст «71.3k» → 71300 с флагом `stars_html_approx` | `a[href$='/stargazers']` |
| описание | первый `p` вне `h3` | текст, нормализация пробелов | — |
| теги | `a.topic-tag`, `a[href^='/topics/']` | тексты ссылок | — |
| язык | `[itemprop=programmingLanguage]` | текст | — |
| дата «Updated» | `relative-time[datetime]` | атрибут `datetime` (ISO 8601, UTC) | `time[datetime]` |
| пагинация | `form.ajax-pagination-form[action]` | наличие ссылки на `page=N+1`; стоп — нет карточек или нет следующей страницы | текст «Load more» |
| всего в теме | текст «Here are N public repositories» | регулярное выражение | — |

---

## 4. Живая демонстрация на защите

| Сценарий | Команда | Что видно |
|---|---|---|
| Быстрый инкрементальный прогон (30–60 с) | `bash demo/demo_incremental.sh` | 1 страница темы + 20 условных запросов к API → ответы 304, UPSERT `+0 / 0 / N`, отчёт о прогоне, расход квоты на 304 |
| Устойчивость: недоступный источник | `bash demo/demo_failure.sh` (`FAULT=html` — отказ HTML) | неверный адрес `api.github.invalid` → повторы с backoff → предохранитель → инцидент `logs/incidents/*.json` → код **3**; mart строится на ранее собранных метриках |
| Возобновляемость (запасной) | `bash demo/demo_resume.sh` | обрыв после N запросов (код 130) → повторный запуск продолжает с места остановки |

Почему не `httpbin.org/status/503` в качестве адреса API: к нему пайплайн дописал бы путь `/repos/…`, и httpbin
вернул бы 404 (ошибка клиента, не повторяется), а не отказ источника; неверный адрес `.invalid` (RFC 2606) даёт
именно сетевой отказ. Отказ 503 и 429 с `Retry-After` воспроизводит локальная имитация
(`/__control?html_status=503&api_429_every=7`).

Резерв на случай отсутствия сети: логи, отчёты и скриншоты реального прогона (`logs/`, `reports/`, `nocode/screenshots/`)
и офлайн-прогон на имитации: `bash demo/run_mock_e2e.sh`.

---

## 5. Проверка без сети: локальная имитация

`demo/mock_github.py` воспроизводит github.com/topics и api.github.com (ETag/304, X-RateLimit-*, 301 при
переименовании, Search API с Link, отказы 503/429 по команде). **Все данные имитации синтетические** (владельцы
`fx-*`), цифры по ним — не результат исследования. `bash demo/run_mock_e2e.sh` прогоняет всё в отдельном каталоге
`demo/_mock_run/` (реальные данные не затрагиваются).

Результаты последней проверки **(на локальной имитации, ускорение лимитов `--rate-scale 100`, разрешённое только для 127.0.0.1)**:

| Прогон | Что проверяет | Итог |
|---|---|---|
| 1 | первичная загрузка | 600 карточек, 557 ответов API 200, чистый слой 556 репозиториев, 1 запись отбракована (дата создания из будущего, внедрена в имитацию намеренно), UPSERT +556 |
| 2 | повтор сразу | 0 запросов к API (водяной знак), UPSERT 0 / 0 / 556 |
| 3 | данные v2, перепроверка | 538 ответов 304 (один — после редиректа переименованного репозитория) и 19 ответов 200, UPSERT 0 / 19 / 537; расход квоты на 304 — 0 (имитация воспроизводит правило GitHub; на реальном API проверяется заново) |
| инкрементальный | одна тема | 20 из 20 ответов 304 |
| обрыв + возобновление | `--simulate-crash-after 8` | код 130, затем продолжение без повторного запроса собранных страниц |
| отказ API | неверный адрес | повторы → предохранитель → инцидент, код 3 |
| pytest | | 55 тестов пройдено, 1 пропущен (реальная страница ещё не сохранена) |
| analysis.ipynb | исполнение через nbclient | 0 ошибок |

---

## 6. Соответствие требованиям

| Требование ТЗ | Где / как выполнено |
|---|---|
| **2.1** ≥ 2 источника: HTML + REST API | github.com/topics (HTML) + api.github.com (REST), связка № 5 из списка |
| ≥ 500 записей в чистом слое | 6 тем × 5 стр. × 20 = 600 карточек, ≈ 560 репозиториев; метрика `clean_repos_count` (зелёный ≥ 500) — реальный прогон: 600 карточек, 592 репозитория |
| Связь источников | обогащение карточек метриками API по ключу `owner/repo`; синонимы при переименовании (`repo_aliases`); `v_repo_topic` |
| **2.2** Паспорт каждого источника | `docs/decisions.md` §2, `config.yaml`, `analysis.ipynb` §1.1 |
| Протокол диагностики HTML (нужен ли браузер) | `python src/collect_html.py --diagnose` → `reports/diagnostics_latest.json`; `docs/decisions.md` §3; блокнот §2.2 |
| Вежливость: лимит на домен, джиттер, Crawl-delay, User-Agent с контактом | `http_client.RateLimiter` (token bucket на ключ домена, джиттер 30 %), `register_limit(..., crawl_delay)`, UA `PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)` |
| Устойчивость: классификация, повторы только временных, Retry-After, предохранитель | `classify` (в т.ч. лимиты GitHub 403 + `X-RateLimit-Remaining: 0`), `backoff_delay` full jitter, `server_requested_wait` (Retry-After > X-RateLimit-Reset > backoff), `RateLimiter.penalize` после 429/503, `CircuitBreaker` на домен |
| ≥ 2 механизма снижения нагрузки | 1) условные запросы ETag → 304; 2) водяной знак (`state/watermark.json`, `select_keys`); 3) gzip (трафик по сети vs тело — в отчёте); 4) кэш HTML для отладки (`--debug-cache`); sitemap у GitHub отсутствует |
| **2.3** raw JSONL (дозапись, провенанс) / clean Parquet / mart | `raw/*.jsonl` с `_run_id,_source,_collected_at,_url,_status,_parser_version,_html_sha256,_etag`; `clean/*.parquet`; `mart/*.parquet/csv` |
| Контракт pydantic, ≥ 2 собственных валидатора, rejects с причиной | `src/contract.py`: `full_name_format`, `topics_are_slugs`, `license_normalized`, `tz_aware`, `temporal_and_key_consistency`, `topic_slug`, `consistency`; `clean/rejects.jsonl` + таблица `rejects` |
| БД: PK по бизнес-ключу, NOT NULL, CHECK, индексы, служебные поля | `db/schema.sql`: `repos(repo_key PK, repo_id UNIQUE)`, `topic_listings(PK topic, repo_key)`, CHECK на диапазоны/регистр/JSON, 6 индексов, `source, collected_at, updated_at, parser_version, first_seen_at, row_hash, run_id` |
| UPSERT + три прогона | `storage.upsert` (`ON CONFLICT DO UPDATE ... WHERE row_hash IS NOT excluded.row_hash`), прогоны 1–3 в `run_real.sh`, таблица `pipeline_runs`; `docs/decisions.md` §6 — 592/0/0 → 0/10/582 → 0/37/555 |
| ≥ 6 метрик качества, пороги, вердикт, блокировка mart | `src/quality.py`: 9 метрик, `config.yaml → quality`; красный → `mart_blocked`, код 2 |
| **2.4** Структурные логи по каждому запросу и событию | `logs/run_<id>.jsonl`: `request` (код, класс, попытка, мс, байты тела и по сети, X-RateLimit-*), `robots_checked`, `html_page_parsed`, `api_selection`, `upsert`, `quality_evaluated`, `incident`, … |
| Отчёт о прогоне | `reports/run_report_<id>.json`: запросы, коды, доля ошибок, повторы, латентность медиана/p95, трафик, записи собранные/отброшенные, длительность, квота, инциденты |
| ≥ 4 порогов оповещения, вкл. сравнение с прошлым прогоном | `config.yaml → alerts` (7): доля ошибок, 403/429, падение числа карточек к прошлому прогону того же охвата, p95, остаток квоты, срабатывания предохранителя, серия прогонов без изменений |
| Расписание с обоснованием | n8n Schedule Trigger `17 3 * * *` UTC; обоснование — `docs/decisions.md` §7, `nocode/notes.md` |
| Возобновляемость | `state/checkpoint.json` (страницы и ключи), водяной знак, ETag, атомарная запись состояния; `demo/demo_resume.sh` |
| **2.5** No-code компонент | `nocode/workflow.json` (n8n: расписание → Execute Command → разбор отчёта → IF → оповещение → Stop and Error; Error Trigger), `nocode/notes.md` (таблица «этап → код / no-code → что не перенесено»), скриншоты — ⟪ЗАПОЛНИТЬ⟫ (`nocode/INSTRUCTIONS.md`) |
| **2.6** 2–3 вывода, ≥ 2 графика, ограничения | `analysis.ipynb` §8 (выводы формируются из вычислений, 3–4 графика в `mart/fig*.png`); `docs/decisions.md` §8 — три вывода по реальным данным |
| **2.7** README воспроизведения | этот файл |
| Схема пайплайна | `docs/pipeline_diagram.md` (Mermaid) |
| Правовой чек-лист (12 пунктов) | `docs/legal_checklist.md` |
| Data card | `docs/data_card.md` |
| **Раздел 3** структура | соответствует (+ `tests/`, `demo/`, общие `http_client.py`, `settings.py`); `slides.pdf` собирается из `slides.md` |
| **Раздел 5** два сценария демонстрации | `demo/demo_incremental.sh`, `demo/demo_failure.sh` (+ `demo_resume.sh`, резерв — `run_mock_e2e.sh`) |
| **Раздел 6** повышенная сложность | тесты pytest на фикстурах (парсер, robots, повторы, контракт, UPSERT); снапшоты `repo_snapshots` для динамики за период (график после ≥ 2 дней прогонов); оркестрация n8n с ветками ошибок |

---

## 7. Риски и что подтвердить первым реальным прогоном

1. **Классы и атрибуты разметки github.com/topics** (таблица селекторов выше) воспроизведены по знанию вёрстки, а не
   сняты с реальной страницы (из среды разработки github.com недоступен). Проверка: шаг 4 `run_real.sh`
   (`tests/fixtures/real_topic_p1.html`, тест `test_real_page_if_present`), в отчёте — `html_topics.*.strategy`
   (ожидается `article-cards`). Если сработал фолбэк или 0 карточек — красное оповещение `html_no_cards`.
2. **Параметр сортировки `s=updated`** (опция `sort_modes`) не подтверждён; по умолчанию не используется.
3. **304 и квота.** По документации GitHub ответ 304 на авторизованный запрос не расходует основную квоту; отчёт
   проверяет это по `X-RateLimit-Used` (`requests.api_quota.used_delta_by_status`). Без токена 304 квоту расходует.
4. **ETag страниц HTML**: страницы GitHub содержат одноразовые токены, поэтому 304 для HTML не ожидается; механизм
   включён и измеряется, основной эффект условных запросов — на API.
5. **n8n ≥ 2.0**: узел Execute Command отключён по умолчанию — запуск с `NODES_EXCLUDE="[]"`; n8n 2.43 требует Node.js ≥ 24, при Node 20–24 — `npx n8n@1` (см. `nocode/INSTRUCTIONS.md`). Сам n8n в среде разработки не запускался (установка не прошла по памяти); имена параметров и версии узлов `workflow.json` сверены с исходниками `n8n-nodes-base` 2.43.4.
6. Цифры в `docs/decisions.md`, `slides.md`, `docs/data_card.md` заполнены по реальному прогону 15.10.2026. При реальном
   прогоне найдено и исправлено: тема `telegram-bot` не существует (404) → `telegram`; ETag `api.github.com` нестабилен →
   условный запрос к API по `If-Modified-Since` (`docs/decisions.md` §7).

## 8. Что осталось сделать вручную

Цифры отчётных документов заполнены по реальному прогону 15.10.2026. Остались действия, которые требуют интерфейса:

| Файл | Что сделать |
|---|---|
| `nocode/INSTRUCTIONS.md`, `nocode/screenshots/` | запустить n8n, импортировать `workflow.json`, выполнить, сделать скриншоты `screenshot_*.png`, вписать версию `n8n --version` |
| `docs/decisions.md` §3 | скриншоты протокола диагностики: `docs/screenshot_view_source.png` (View Source, поиск имени репозитория) и `docs/screenshot_network.png` (DevTools → Network при нажатии «Load more…») |
| `slides.md` → `slides.pdf` | собрать PDF из содержания слайдов (можно попросить Claude) |
