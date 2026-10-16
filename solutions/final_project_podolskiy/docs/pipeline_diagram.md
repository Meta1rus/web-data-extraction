# Схема пайплайна

Конфигурация B: n8n отвечает за расписание, запуск и оповещения; сбор, очистка, качество и хранение — код.

```mermaid
flowchart LR
    subgraph N8N["n8n (nocode/workflow.json)"]
        ST["Schedule Trigger<br/>17 3 * * * UTC"] --> PRM["Set: параметры запуска"]
        MT["Manual Trigger (демо)"] --> PRM
        PRM --> EXE["Execute Command<br/>python src/run_pipeline.py --quiet"]
        EXE -->|stdout| PARSE["Code: разбор RUN_SUMMARY<br/>и PIPELINE_EXIT_CODE"]
        EXE -->|ошибка узла| FAILSET["Set: сбой запуска"]
        PARSE --> IFA{"вердикт ≠ green<br/>или код ≠ 0?"}
        IFA -->|нет| OK["NoOp: прогон здоров"]
        IFA -->|да| MSG["Set: текст оповещения"]
        FAILSET --> MSG
        MSG --> FILE["Convert to File → Write<br/>~/.n8n-files/podolskiy/alert_*.json"]
        MSG --> TG["Telegram (выключен до настройки)"]
        FILE --> IFR{"красный?"}
        IFR -->|да| STOP["Stop and Error<br/>(выполнение помечено сбоем)"]
        ET["Error Trigger"] --> EMSG["Set → Convert → Write<br/>workflow_error_*.json"]
    end

    subgraph SRC["Источники"]
        H["github.com/topics/&lt;topic&gt;?l=python&page=N<br/>HTML, 20 карточек на странице"]
        A["api.github.com/repos/{owner}/{repo}<br/>REST JSON, ETag"]
    end

    subgraph PIPE["Код: src/run_pipeline.py"]
        R["robots.txt<br/>RFC 9309 + urllib.robotparser"] --> CH["collect_html.py<br/>парсер (3 стратегии), детектор блокировки"]
        CH -->|ключи owner/repo + дата Updated| SEL["collect_api.py: отбор по водяному знаку<br/>state/watermark.json"]
        SEL --> CA["условный GET If-None-Match<br/>state/etags.json → 200 / 304"]
        CL["clean.py: протокол очистки<br/>дедупликация, вычисляемые поля"] --> CT["contract.py (pydantic)<br/>валидные / rejects с причиной"]
        CT --> DB["storage.py: SQLite UPSERT<br/>repos, topic_listings, aliases, snapshots, rejects"]
        DB --> Q["quality.py: 9 метрик → вердикт"]
        Q -->|green / yellow| M["mart/: v_repo_topic, topic_summary,<br/>size_band_topic, activity_band_topic"]
        Q -->|red| BLK["mart не обновляется, код 2"]
        OBS["observability.py: JSON-лог каждого запроса,<br/>отчёт о прогоне, 7 порогов оповещения, инциденты"]
    end

    EXE -.запускает.-> R
    H --> CH
    A --> CA
    CH --> RAWH[("raw/github_topics_html.jsonl<br/>+ raw/html_pages/*.html.gz")]
    CA --> RAWA[("raw/github_api_repos.jsonl")]
    RAWH --> CL
    RAWA --> CL
    CT --> CLEAN[("clean/*.parquet<br/>clean/rejects.jsonl")]
    M --> NB["analysis.ipynb: выводы, графики"]
    OBS --> REP[("reports/run_report_*.json<br/>logs/run_*.jsonl, logs/incidents/")]
    REP -.RUN_SUMMARY.-> PARSE
```

## Сквозной поток одной записи

| Шаг | Где | Что происходит с репозиторием `owner/repo` |
|---|---|---|
| 1 | `collect_html.collect_topics` | карточка на странице темы → поля как есть + `rank_in_topic` → строка в `raw/github_topics_html.jsonl` с провенансом |
| 2 | `collect_api.select_keys` | ключ новый / дата «Updated» новее водяного знака / проверка старше 20 ч → в очередь API; иначе пропуск |
| 3 | `PoliteClient.get` | лимит домена → `If-None-Match` → 304 (только отметка `checked_at`) или 200 (минимизированный JSON в raw) |
| 4 | `clean.build_clean` | нормализация текста, ключ в нижнем регистре, типы и UTC, самая свежая версия, синоним при переименовании |
| 5 | `contract` | `TopicListing` / `RepoRecord`; нарушение → `clean/rejects.jsonl` с полем и причиной |
| 6 | `storage.load_all` | UPSERT по `repo_key`; `updated_at` меняется только при смене хэша бизнес-полей; снапшот дня |
| 7 | `quality.compute_quality` | метрики набора; красный статус блокирует mart |
| 8 | `storage.build_mart` | `v_repo_topic` (карточка ⋈ метрики API) → агрегаты по темам, размеру и активности |

## Состояние между прогонами

| Файл / таблица | Назначение |
|---|---|
| `state/watermark.json` | по каждому ключу: `checked_at`, `pushed_at`, `gh_updated_at`, код последнего ответа; глобальный `api_max_updated_at` |
| `state/etags.json` | ETag / Last-Modified по URL для условных запросов |
| `state/checkpoint.json` | незавершённый прогон: собранные страницы тем и обработанные ключи API (возобновление) |
| `pipeline_runs` (БД) | история прогонов: сравнение с прошлым, серия «без изменений», признак имитации по адресам источников |
