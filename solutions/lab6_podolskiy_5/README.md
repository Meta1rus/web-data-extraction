# Лабораторная 6 — Хранение, очистка и структурирование собранных данных

**Вариант 5 — «Репозитории и активность».** Источники: GitHub REST API (репозитории, issues и pull requests) и HTML-страницы github.com (страница проекта и список релизов). Целевая схема: репозиторий (1) → события (N), все даты в UTC.

Конвейер: **raw** (JSON Lines, только дозапись) → **clean** (6 шагов очистки + контракт pydantic → Parquet) → **SQLite** (UPSERT, SCD2, ограничения) → **метрики со «светофором»** → **mart** (представления, партиции по дате, графики). No-code часть — n8n → Postgres Upsert.

Набор — 12 репозиториев инструментов курса из `config/repos.txt`: scrapy, parsel, requests, httpx, aiohttp, playwright-python, selenium, n8n, pandas, pydantic, arrow, duckdb. Почему именно они — в `report.md`, раздел 2.

> **Статус данных.** В среде разработки `api.github.com` и `github.com` были закрыты сетевой политикой (HTTP 403 от прокси, ответ не от GitHub), поэтому папки `raw/`, `clean/`, `mart/`, `db/*.db` пустые. Их заполнит реальный прогон `./run_real.sh`. Код проверен end-to-end на локальной имитации GitHub (`tests/mock_github.py` + синтетические фикстуры `tests/fixtures/`), см. раздел «Проверка».

## Состав

```
lab6_podolskiy_5/
├── raw/                      # заполняется сбором; файлы 0444, только дозапись
│   ├── github_api.jsonl      #   _entity = repo | issue
│   ├── github_html.jsonl     #   _entity = repo_page | release (+ raw/html/<run_id>/*.html.gz)
│   ├── synthetic_dirty.jsonl #   синтетическая порча 7 %, _synthetic = true
│   └── MANIFEST.jsonl        #   размер и SHA-256 после каждой дозаписи
├── clean/                    # dataset.parquet (события), repositories.parquet, rejects.jsonl,
│                             # cleaning_log.csv (протокол), format_benchmark.csv
├── mart/                     # views/*.parquet, snapshots/dt=YYYY-MM-DD/, charts/*.png
├── db/
│   ├── schema.sql            # таблицы, CHECK, индексы, SCD2, rejects, представления mart
│   ├── load.py               # UPSERT + SCD2; --demo — три прогона и отказ ограничений
│   ├── warehouse.db          # рабочая база (после прогона)
│   └── demo_idempotency.db   # база демонстрации трёх прогонов (после прогона)
├── src/
│   ├── collect.py            # сбор API + HTML: robots.txt, паузы, лимиты, Link-пагинация
│   ├── parsers.py            # селекторы HTML, «54.3k» → 54300, slug, канонизация URL
│   ├── robots.py             # robots.txt: RFC 9309 (шаблоны * и $) + urllib.robotparser
│   ├── rawio.py              # дозапись JSONL + MANIFEST + проверка неизменности
│   ├── corrupt.py            # синтетическая порча 5–10 % (отдельный файл, с пометкой)
│   ├── clean.py              # шесть шагов очистки с протоколом + контракт
│   ├── contract.py           # модели pydantic Repository / Event + пробы на битых записях
│   ├── rebuild.py            # пересборка clean из raw без сети (с проверкой отпечатков)
│   ├── reparse_html.py       # перепарсинг сохранённых HTML новой версией парсера
│   ├── formats.py            # CSV vs JSON Lines vs Parquet
│   ├── quality.py            # 10 метрик, пороги, вердикт, стоп-кран
│   ├── mart.py               # представления, партиции, графики
│   ├── duckdb_bench.py       # DuckDB по Parquet против SQLite (повышенная сложность)
│   ├── pipeline.py           # всё после сбора, по порядку
│   └── fill_report.py        # подстановка измеренных цифр в report.md
├── config/repos.txt
├── quality_report.ipynb      # протокол, метрики, графики, выводы (сохранён без выводов)
├── nocode/                   # workflow.json, INSTRUCTIONS.md, pg_init.sql, docker-compose.yml
│                             # (+ screenshot_*.png и nocode_result.csv после прогона в n8n)
├── tests/                    # pytest, имитация GitHub, синтетические фикстуры
├── report.md                 # отчёт; цифры в блоках AUTO подставляются после прогона
├── run_real.sh               # реальный прогон одной командой
├── requirements.txt
└── .env.example
```

## Реальный прогон (macOS)

```bash
cd lab6_podolskiy_5
cp .env.example .env          # по желанию вписать GITHUB_TOKEN
./run_real.sh                 # venv + зависимости → сбор → порча → конвейер → report.md → блокнот
```

`run_real.sh` создаёт `.venv`, если её нет, пишет полный лог в `logs/real_run_<UTC>.log` и в конце печатает фактическое время.

**Ожидаемое время** (оценка по паузам, не замер):
- сбор — 2–4 мин: 48 запросов к API с паузой ≥ 1 с и 25 HTML-страниц с паузой ≥ 3 с;
- конвейер и блокнот — меньше минуты;
- установка зависимостей при первом запуске — 1–3 мин.

Без токена прогон укладывается в лимит 60 запросов/ч: 12 × (1 + 3) = 48 запросов. Повторный запуск в течение часа упрётся в лимит. Тогда сбор сохранит уже полученное и завершится с кодом 3, а конвейер продолжит работу. Повторные запуски идут с `--since auto`: из API берутся только события, изменённые после прошлого сбора.

Код выхода: `0` — успех; `2` — «светофор» красный и mart не обновлён (причина в логе и в `report.md`, раздел 9).

### Пошагово

```bash
source .venv/bin/activate
python -m src.collect --repos config/repos.txt --max-issue-pages 3   # сбор → raw/
python -m src.corrupt --share 0.07 --seed 5                          # порча → raw/synthetic_dirty.jsonl
python -m src.clean                                                  # 6 шагов → clean/ + протокол
python -m src.rebuild --verify                                       # пересборка без сети, отпечатки до/после
python -m src.formats                                                # clean/format_benchmark.csv
python db/load.py                                                    # UPSERT в db/warehouse.db
python db/load.py --demo                                             # три прогона + отказ ограничений
python -m src.quality                                                # метрики и вердикт (код 2 при красном)
python -m src.mart                                                   # mart/ (только если не красный)
python -m src.duckdb_bench                                           # DuckDB vs SQLite
python -m src.fill_report                                            # цифры → report.md
jupyter nbconvert --to notebook --execute --inplace quality_report.ipynb
```
`python -m src.pipeline` выполняет шаги с `clean` по `duckdb_bench` подряд.

### Переменные окружения

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `GITHUB_TOKEN` | токен GitHub (необязателен; в выводе маскируется) | — |
| `MAX_ISSUE_PAGES` | страниц `/issues` по 100 записей на репозиторий | 3 |
| `PYTHON` | интерпретатор для создания `.venv` | `python3` |
| `LAB6_DATA_ROOT` | куда писать raw/clean/mart/db/logs — **только для тестов** | корень лабораторной |
| `GITHUB_API_URL`, `GITHUB_WEB_URL` | подмена адресов — **только для имитации** (`run_real.sh` их сбрасывает) | `https://api.github.com`, `https://github.com` |
| `LAB6_PG_PASSWORD` | пароль Postgres для no-code (docker compose) | `lab6_local_only` |

## Источники и правила извлечения

**API** (`src/collect.py`): `GET /rate_limit` (не расходует лимит, по нему планируется бюджет), `GET /repos/{o}/{r}`, `GET /repos/{o}/{r}/issues?state=all&sort=updated&direction=desc&per_page=100` со ссылкой `Link: rel="next"`. Заголовки: `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `User-Agent: PSU-edu-crawler/1.0 (+…; student lab)`. Повторы при 5xx и сетевых ошибках, ожидание по `Retry-After` / `X-RateLimit-Reset`. В raw попадают только поля целевой схемы: логины, аватары и тексты issues отбрасываются ещё при сборе. PR из выдачи `/issues` маркируются признаком `is_pull_request`.

**HTML** (`src/parsers.py`). Значения сохраняются строкой «как на странице» (`*_raw`); в числа их превращает очистка.

| Поле | Селектор | Запасной путь | Как получаем значение |
|---|---|---|---|
| `repository_id_raw`, `repo_nwo_raw` | `meta[name=octolytics-dimension-repository_id / _nwo]` | — | атрибут `content`; сверяется с `id` из API (правило R-04) |
| `about_raw` | `h2` «About» → ячейка `.BorderGrid-cell` → `p.f4` | `meta[name=description]` без хвоста « - owner/repo» | текст |
| `homepage_raw` | в ячейке About `a[role=link][href^=http]` | `span.css-truncate a[href^=http]` | `href` |
| `topics_raw` | `a.topic-tag` | — | список текстов |
| `license_raw` | ссылка с `svg.octicon-law` | ссылка с текстом «… license» | текст |
| `stars_raw` / `stars_title_raw` | встроенный JSON `payload.sidebarAbout.stargazerCount` (React-страница, основной путь с 15.10.2026); прежде `#repo-stars-counter-star` | `a[href$="/stargazers"] strong` в About | текст «55.1k» и точное `title` «55,120» |
| `forks_raw` / `forks_title_raw` | `#repo-network-counter` | `a[href$="/forks"] strong` | то же |
| `watchers_raw` | `a[href$="/watchers"] strong` | — | текст |
| `releases_count_raw`, `latest_release_*` | `h2` «Releases» → `.Counter`; `a[href*="/releases/tag/"]`, `relative-time[datetime]` | тег из `href` | число, тег, дата ISO |
| `used_by_raw` / `_title_raw` | `h2` «Used by» → `.Counter` (text / `title`) | — | «54.1k» / «54,118» |
| `contributors_raw` / `_title_raw` | `h2` «Contributors» → `.Counter` | — | «592»; «5,000+» → нижняя граница |
| `languages_raw` | `h2` «Languages» → `li`: `span.text-bold` + соседний `span` | `span.Progress-item[aria-label]` | «Python 99.8%» |
| релиз: `tag_raw`, `title_raw` | `section` → `a[href*="/releases/tag/"]` | без `section`: `a.Link--primary[href*="/releases/tag/"]` + ближайший предок с датой | тег из `href` (URL-декод), текст ссылки |
| релиз: `datetime_raw`, `labels_raw`, `commit_sha_raw` | `relative-time / local-time[datetime]`, `span.Label`, `a[href*="/commit/"] code` | `code` | ISO, «Latest» / «Pre-release», короткий SHA |
| пагинация релизов | `a.next_page`, `a[rel=next]` | — | по умолчанию берётся 1 страница (10 релизов) |

**robots.txt** (`src/robots.py`). Разрешены `/{o}/{r}` и `/{o}/{r}/releases`. Сам сборщик проверяет ещё 5 контрольных адресов: `/contributors`, `/stargazers`, `/network/dependents`, `/tree/main`, `?tab=`. Решения пишутся в `logs/robots_decisions_<run>.json`. Адрес разрешён, только если его разрешают и разбор по RFC 9309, и `urllib.robotparser`. Без собственного разбора нельзя: в Python 3.11 `urllib` шаблонов `*` не понимает, а в 3.13.16 понимает, но адрес с `?tab=` всё равно пропускает. Если robots.txt недоступен (5xx), обход запрещается — так требует RFC 9309.

## Соответствие требованиям

| Требование задания | Где и как выполнено |
|---|---|
| raw: JSON Lines, только дозапись | `src/rawio.py`: режим `"a"`, права 0444 между дозаписями, `MANIFEST.jsonl` с SHA-256; `verify_all()` перед сбором, очисткой и пересборкой (`RawTamperedError`) |
| raw: `_source`, `_source_url`, `_collected_at` (UTC ISO 8601), `_parser_version`, `_status` | `collect.meta()` (+ `_entity`, `_run_id`, `_final_url`, `_html_file`) |
| HTML: сырое значение всех целевых полей | `*_raw` в `parsers.parse_repo_page` / `parse_releases_page`; сами страницы в `raw/html/<run_id>/*.html.gz` |
| пересборка clean из raw без сайта + демонстрация | `src/rebuild.py --verify` (сеть заблокирована, отпечатки до/после); `src/reparse_html.py`; отчёт, разд. 3 |
| ≥ 2 источника разной природы (HTML + API), ≥ 300 записей в clean | API (repo, issue/PR) + HTML (страница, релизы); 12 × до 300 событий + 120 релизов |
| сравнение CSV / JSONL / Parquet: размер, чтение, 2–3 столбца, типы, дата, ведущий ноль | `src/formats.py` → `clean/format_benchmark.csv`; `label_color` «008672», SHA «0…», ловушка партии из одних цифр; вывод — отчёт, разд. 4 |
| 6 шагов очистки в порядке, протокол с числами | `src/clean.py` (`step1_text` … `step6_rules`) → `clean/cleaning_log.csv` (до / после / изменено / удалено / в rejects / примечание) |
| бизнес-ключ, URL с трекингом нормализуется | `repo_slug()`, `canonical_url()`; PK — `owner/name` и `owner/name#номер` / `@тег`, не URL |
| типизация: числа из текста, nullable, даты UTC | `parse_compact_number`, `Int64`, `boolean`, `parse_utc()` — наивные даты не угадываются |
| пропуски: обязательные → rejects, необязательные → NULL, без среднего | `step4_missing`; полнота необязательных — в протоколе и метрике M2 |
| дубли: полные, по ключу, почти-дубли, свежая запись | `_dedup()`: `_raw_hash`, сортировка по `updated_at`/`_collected_at`, классификация «почти-дубль / повтор / устаревшая» |
| ≥ 5 бизнес-правил | 14 правил: R-01…R-06 (репозитории, в т. ч. сверка HTML↔API), E-01…E-08 (события) → `logs/rule_checks.csv` |
| контракт pydantic: типы, обязательность, `gt`/`le`/`min_length`, ≥ 2 `field_validator`, ≥ 1 `model_validator` | `src/contract.py`: 4 `field_validator` + `model_validator` в каждой модели |
| демонстрация на ≥ 5 битых записях, rejects с полем и причиной | `contract.run_probes()` → 15 проб (`logs/contract_probes.csv`); `clean/rejects.jsonl` (этап, поле, причина, payload) |
| БД: `schema.sql`, PK по бизнес-ключу, NOT NULL, CHECK, UNIQUE, индексы | `db/schema.sql`; даты проверяются через `strftime(...)`, частичные уникальные индексы, индексы под запросы mart |
| служебные поля `source`, `source_url`, `parser_version`, `collected_at`, `updated_at` | во всех основных таблицах (+ `row_hash`) |
| UPSERT `ON CONFLICT DO UPDATE`, `executemany` | `db/load.py`: `upsert_sql()` с `WHERE row_hash IS NOT excluded.row_hash` |
| таблица rejects с payload и причиной | `rejects` (JSON-payload, UNIQUE от дублей) |
| история: снапшоты или SCD2 с обоснованием | SCD2 `repositories_history`, `events_history` (+ снапшоты `dt=` в mart); обоснование — отчёт, разд. 7 |
| три прогона с цифрами, `updated_at` только при изменениях | `python db/load.py --demo` → `logs/load_demo.json`, отчёт, разд. 8 |
| отказ CHECK и дублирующегося ключа | `constraints_demo()`: 8 попыток (CHECK, PK, UNIQUE, FK) → все отклонены |
| ≥ 7 метрик, пороги с обоснованием, вердикт, прерывание при красном | `src/quality.py`: 10 метрик, `THRESHOLDS` с `why`, `enforce_gate()` → `QualityGateError`; демонстрация `demo_red_gate()` |
| mart: ≥ 3 представления, партиции по дате, чтение одной партиции, ≥ 2 графика | `v_repo_activity`, `v_weekly_activity`, `v_top_repos` → `mart/views/*.parquet`; `snapshots/dt=…`; `read_partition()`; 3 графика в `mart/charts/` |
| no-code: нормализация 3+ полей, Remove Duplicates, IF с отбраковкой, Upsert, расписание | `nocode/workflow.json` (Set: 14 полей, Remove Duplicates, IF → `rejects_n8n`, Postgres Upsert, Schedule Trigger); `INSTRUCTIONS.md`, `pg_init.sql` |
| таблица «код vs no-code» с нереализуемым | отчёт, разд. 11 |
| правовой раздел (152-ФЗ / GDPR, минимизация, место и срок хранения) | отчёт, разд. 12 |
| штрафы: правка raw, CSV как хранилище, PK по URL, среднее, тихое удаление, INSERT вместо UPSERT, float для денег, наивные даты | raw неизменяем и проверяется; CSV только для обмена; PK — бизнес-ключ; пропуски → NULL; всё отбракованное в rejects; UPSERT; денег в варианте нет; наивные даты отвергаются очисткой, контрактом и CHECK |
| повышенная сложность | **SCD2** с `valid_from` / `valid_to` / `is_current` и запросом «на дату» (`db/load.py: asof()`); **DuckDB над Parquet** (`src/duckdb_bench.py`); частично **автоматизация контроля**: расписание n8n и метрика объёма к прошлому прогону (`logs/quality_history.jsonl`), уведомления нет |

## Проверка (что сделано без доступа к GitHub)

```bash
python -m pytest -q tests        # парсеры, robots, контракт, e2e на имитации (на имитации шло 20–40 с)
```
- `tests/make_fixtures.py` генерирует **синтетические** фикстуры: JSON в формате ответов API и HTML по известной разметке github.com, 12 репозиториев, ≈3,3 тыс. issues/PR, 240 релизов. Значения выдуманы, их нельзя выдавать за реальные.
- `tests/mock_github.py` поднимает на 127.0.0.1 имитацию API (Link-пагинация, `X-RateLimit-*`, 403 при исчерпании лимита, разовый 502 для проверки повторов) и сайта (robots.txt, страницы, `?page=N`).
- e2e-тест прогоняет `collect → corrupt → pipeline → fill_report` во временной папке и проверяет:
  - ≥ 300 событий в clean, в rejects только синтетика;
  - все даты tz-aware UTC;
  - идемпотентность (0 обновлений во втором прогоне, 2 — в третьем) и отказ всех 8 ограничений;
  - вердикт не красный;
  - обнаружение правки raw.
- `quality_report.ipynb` исполнен через nbclient на имитации без ошибок и сохранён без выводов.
- `nocode/workflow.json` импортирован и выполнен в n8n 1.123.85 и 2.35.7 (CLI) против имитации API и PostgreSQL 16 с `pg_init.sql`. Два прогона подтвердили дедупликацию, отбраковку и Upsert без лишних обновлений (подробности — `nocode/INSTRUCTIONS.md`, раздел 7).
- Совместимость проверена на Python 3.11 с нижними границами `requirements.txt` (pandas 2.2) и на Python 3.13 с pandas 3.0.

Если данные получены на имитации (адреса 127.0.0.1), `fill_report` ставит в каждый блок отчёта пометку «(на локальной имитации)».

## Риски: что подтвердить первым реальным прогоном

1. **Разметка github.com — проверено реальным прогоном 15.10.2026.** Страница репозитория теперь React-приложение: прежние селекторы (`#repo-stars-counter-star`, `.BorderGrid-cell` + `.Counter`) на ней пусты, а данные About лежат во встроенном JSON `script[data-target="react-app.embeddedData"]` → `payload.sidebarAbout`. Парсер дополнен этим путём (`github-html/1.1`), сохранённые страницы перепарсены офлайн (`python -m src.reparse_html`). Страница `/releases` серверная, её селекторы подтвердились. Если вёрстка снова поменяется, поломку покажут полнота HTML-полей в протоколе (шаг 4, `repos_html`) и метрика M7 (< 70 % → красный, mart не строится). Порядок исправления: поправить `src/parsers.py`, поднять `HTML_PARSER_VERSION` в `src/config.py`, `python -m src.reparse_html`, затем `python -m src.pipeline`.
2. «Languages», «Used by», «Contributors» и число релизов React-страница подгружает отдельными запросами уже в браузере — в исходном HTML их нет (подтверждено прогоном). Поля необязательны, остаются NULL; снижение полноты видно в M2.
3. Фикстура `robots.txt` восстановлена по памяти. При сборе всегда скачивается настоящий файл, решения пишутся в `logs/robots_decisions_*.json`. Если правила запретят `/releases`, релизы будут пропущены с сообщением.
4. Лимит API без токена — 60 запросов/ч. Повторный запуск в течение часа соберёт неполные данные (код 3) — для отчёта лучше один полный прогон или прогон с токеном.
5. Все места, которые даёт только реальный прогон, помечены ⟪ЗАПОЛНИТЬ⟫:
   - в `report.md` их закрывает `python -m src.fill_report`, `run_real.sh` вызывает его сам;
   - в `nocode/INSTRUCTIONS.md` (раздел 6) — числа из прогона n8n и скриншоты (заполнено по прогону 16.10.2026).
