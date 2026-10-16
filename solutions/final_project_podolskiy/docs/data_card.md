# Паспорт данных (data card)

По мотивам «Datasheets for Datasets». Набор: **Python-репозитории тем GitHub с метриками популярности, активности и размера**.

## 1. Назначение и происхождение

| | |
|---|---|
| Вопрос, на который отвечает набор | Как популярность и активность Python-репозиториев связаны с их тематикой и размером? |
| Владелец проекта, контакт | Подольский Ф. А., ПМИ-3-2025; User-Agent `PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)` |
| Источник 1 (HTML) | `https://github.com/topics/<topic>?l=python&page=1..5`, порядок «Most stars»; темы: web-scraping, fastapi, machine-learning, data-engineering, cli, telegram |
| Источник 2 (API) | `https://api.github.com/repos/{owner}/{repo}` (REST, версия 2022-11-28); без токена — `/search/repositories?q=topic:<t> language:python` |
| Связь | ключ `repo_key = lower(owner/repo)`; при переименовании — синоним через `repo_id` (таблица `repo_aliases`) |
| Сбор | `src/run_pipeline.py`, ежедневно 03:17 UTC (n8n Schedule Trigger); первая загрузка 15.10.2026 |
| Версия парсера / нормализатора | `gh-topics-1.0.0` / `gh-repo-1.0.0` (поле `parser_version` в каждой записи) |

## 2. Состав: сущности и поля

### `repos` — репозиторий (источник: API), ключ `repo_key`

| Поле | Тип | Единица / домен | Происхождение (поле API) | Обязательность |
|---|---|---|---|---|
| repo_key | TEXT | owner/repo, нижний регистр | `full_name` | PK |
| repo_id | INTEGER | id GitHub | `id` | обязательно, UNIQUE |
| full_name, owner_login, owner_type | TEXT | User / Organization | `full_name`, `owner.login`, `owner.type` | обязательно (кроме owner_type) |
| description, homepage | TEXT | — | `description`, `homepage` | нет |
| language | TEXT | основной язык (GitHub Linguist) | `language` | нет (у пустых репозиториев отсутствует) |
| license_spdx | TEXT | SPDX-идентификатор; `NOASSERTION` — лицензия не распознана | `license.spdx_id` | нет |
| topics_json | JSON-массив | slug тем, ≤ 20 | `topics` | обязательно (может быть пустым) |
| stars | INTEGER | шт., ≥ 0 | `stargazers_count` | обязательно |
| forks | INTEGER | шт. | `forks_count` | обязательно |
| open_issues | INTEGER | открытые issues + PR | `open_issues_count` | обязательно |
| subscribers | INTEGER | наблюдатели (watch) | `subscribers_count` (нет в Search API) | нет |
| size_kb | INTEGER | КБ, размер git-репозитория с историей | `size` | обязательно |
| is_archived, is_fork | 0/1 | — | `archived`, `fork` | обязательно |
| created_at, pushed_at, gh_updated_at | TEXT ISO 8601 UTC | — | `created_at`, `pushed_at`, `updated_at` | created/updated — обязательно |
| default_branch, etag | TEXT | — | `default_branch`, заголовок `ETag` | нет |
| source, collected_at, first_seen_at, updated_at, parser_version, run_id, row_hash | служебные | `updated_at` — когда запись **изменилась** в нашей БД | — | обязательно |

### `topic_listings` — карточка на странице темы (источник: HTML), ключ (`topic`, `repo_key`)

| Поле | Тип | Происхождение (разметка) | Примечание |
|---|---|---|---|
| topic | TEXT | URL страницы | slug темы |
| page, rank_in_topic | INTEGER | номер страницы, порядок карточки | позиция в выдаче «Most stars» |
| stars_html, stars_html_approx | INTEGER, 0/1 | `aria-label`/`title` счётчика, иначе текст «71.3k» | `approx = 1`, если взято сокращённое число |
| language_html, description_html, tags_json | TEXT / JSON | `[itemprop=programmingLanguage]`, `p`, `a.topic-tag` | |
| html_updated_at | TEXT ISO UTC | `relative-time[datetime]` | используется для водяного знака |
| first_seen_at, last_seen_at | TEXT ISO UTC | — | когда карточка впервые и последний раз видна в теме |

### Вычисляемые поля (clean / mart)

`age_days`, `days_since_push` (относительно момента сбора), `stars_per_year` = stars / max(age, 30 дн) × 365,25,
`size_mb`, `size_band` (<1, 1–10, 10–100, ≥100 МБ), `activity_band` (push ≤30, 31–90, 91–365, >365 дн), `active_90d`,
`issues_per_100_stars`, `is_python`. В `mart/` вместо `repo_key` — обезличенный `repo_ref` (SHA-256, 16 символов).

### История

`repo_snapshots` — по строке на репозиторий и дату проверки (200/304/search): звёзды, форки, issues, наблюдатели, размер.

## 3. Объём, покрытие, пропуски

| Показатель | Значение |
|---|---|
| Карточек тем за полный прогон | 600 (6 тем × 5 страниц × 20) |
| Уникальных репозиториев в чистом слое | 592 (8 репозиториев входят в несколько тем) |
| Доля Python (основной язык) | 100 % |
| Доля отбракованных записей и причины | 0 % (0 записей) |
| Пропуски: license / subscribers / pushed_at | 7,8 % / 0,0 % / 0,0 % |
| Доля звёзд HTML в сокращённом виде | 0,0 % (звёзды берутся из точного `aria-label`) |
| Период наблюдений | 15.10.2026 — 15.10.2026 (один снапшот) |

## 4. Частота обновления

Ежедневно в 03:17 UTC. Водяной знак: репозиторий перепроверяется, если с прошлой проверки прошло ≥ 20 ч или дата
«Updated» на странице темы новее сохранённой; неизменившиеся отвечают 304. Карточки, не встречавшиеся 7 дней,
в mart не попадают (`listing_max_age_days`).

## 5. Известные ограничения и смещения

1. **Усечение снизу:** в каждой теме — только первые 100 репозиториев по звёздам; малопопулярные проекты не представлены,
   ранговые корреляции внутри тем занижены.
2. **Темы ставят сами авторы** — тема отражает самоописание, а не проверенную классификацию; часть релевантных проектов тему не указала.
3. **Пересечение тем:** репозиторий из нескольких тем учитывается в каждой.
4. **`size`** — размер git-репозитория с историей и бинарными файлами, а не объём кода; у ML-проектов завышен данными/моделями.
5. **Звёзды** — накопленная величина: зависят от возраста проекта; `stars_per_year` частично компенсирует.
6. **`pushed_at`** может быть раньше `created_at` (импорт истории) — это не ошибка, доля фиксируется в отчёте о качестве.
7. **Лаг между источниками:** звёзды на карточке и в API получены в разные моменты (секунды–часы); допуск согласованности 5 %.
8. Без токена (режим search) нет `subscribers`.

## 6. Условия использования источника и лицензия набора

Данные GitHub: GitHub Terms of Service, Acceptable Use Policies (разд. 7 — исследовательское использование публичной
неперсональной информации), документация REST API (лимиты). Содержимое репозиториев не копируется — только метаданные.
Набор используется в учебных целях; сырые слои не публикуются; аналитический слой обезличен (`repo_ref`).
Срок хранения рабочих слоёв — не более 90 дней после защиты (см. `docs/legal_checklist.md`, п. 10).
