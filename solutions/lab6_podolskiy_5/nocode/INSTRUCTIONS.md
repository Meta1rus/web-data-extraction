# No-code часть: n8n → Postgres (Upsert), отбраковка, дедупликация, расписание

> **Как запускать на этом Mac:** n8n запускается одной командой из корня папки курса — `./start_n8n.sh` (в ней уже заданы все переменные окружения). Результаты workflow пишутся в `~/.n8n-files/lab6/`, оттуда их нужно скопировать в `nocode/` этой лабы. Общая пошаговая инструкция — `ИНСТРУКЦИЯ.md` в корне.


Файлы: `workflow.json` (экспорт n8n), `pg_init.sql` (таблицы и триггер), `docker-compose.yml` (Postgres 16).
Результат после прогона: `nocode_result.csv` и скриншоты `screenshot_*.png` в этой папке.

## Что делает workflow

```
Каждый день 06:47 ─┐
Запуск вручную ────┴─► GitHub API: issues scrapy/scrapy ──(ошибка после 3 попыток)──► Ошибка HTTP (Stop and Error)
                              │ 100 последних обновлённых issues/PR
                              ▼
                     Демо: синтетические битые записи (+3 битые, +1 полный дубль, помечены is_synthetic)
                              ▼
                     Нормализация полей (Set: 14 полей — NFKC/zero-width в title, state → lower,
                              │          даты → UTC ISO 8601, URL без параметров, event_key)
                              ▼
                     Дедупликация по event_key (Remove Duplicates)
                              ▼
                     Запись валидна? (IF: title не пуст, created_at есть, comments ≥ 0, closed ⇒ closed_at)
                     да │                                   │ нет
                        ▼                                   ▼
            Upsert в events_n8n (по event_key)     Причина отбраковки (Set) → Upsert в rejects_n8n
                        ▼
            Выгрузка таблицы для отчёта (SELECT, один раз) → В CSV → Сохранить nocode_result.csv
```

## 1. Postgres в Docker

```bash
cd lab6_podolskiy_5
docker compose -f nocode/docker-compose.yml up -d
docker exec -it lab6-postgres psql -U lab6 -d lab6 -c '\dt'      # events_n8n, rejects_n8n, events_n8n_history
```
Пароль по умолчанию — `lab6_local_only` (порт открыт только на 127.0.0.1). Свой пароль задаётся переменной `LAB6_PG_PASSWORD` до первого запуска контейнера.

## 2. Запуск n8n

```bash
mkdir -p ~/.n8n-files/lab6
export N8N_RESTRICT_FILE_ACCESS_TO="$HOME/.n8n-files"     # узел записи файлов работает только внутри этой папки
export N8N_BLOCK_ENV_ACCESS_IN_NODE=false                 # путь к файлу строится из $env.HOME
export GENERIC_TIMEZONE="Asia/Yekaterinburg"              # часовой пояс расписания (Пермь)
npx n8n                                                   # UI: http://localhost:5678
```

## 3. Учётные данные и импорт

1. **Credentials → Add credential → Postgres**, имя `Postgres lab6 (docker)`: Host `localhost`, Port `5432`, Database `lab6`, User `lab6`, Password `lab6_local_only`, SSL `disable`. Нажать **Test** → *Connection tested successfully*.
2. **Workflows → Import from File** → `nocode/workflow.json`.
3. Открыть три узла Postgres (`Upsert в events_n8n`, `Upsert в rejects_n8n`, `Выгрузка таблицы для отчёта`) и выбрать созданный credential. ID в экспорте ссылается на чужую установку, поэтому выбрать его нужно вручную.
4. В узлах Upsert проверить: *Operation* = **Upsert**, *Table* = `events_n8n` / `rejects_n8n`, *Mapping Column Mode* = **Map Automatically**, *Columns to match on* = `event_key` (для rejects — `event_key` и `reason`). Если после импорта поле пустое, выбрать столбец из списка.
5. (Необязательно) Токен GitHub: в узле HTTP Request *Authentication → Generic → Header Auth*, заголовок `Authorization: Bearer <токен>`. Для одного запроса в сутки хватает лимита без токена (60 запросов/ч).

## 4. Прогоны и проверка идемпотентности

1. **Execute workflow**. Ожидаемый поток: 100 → 104 (+4 синтетических) → 103 после Remove Duplicates → 100 в ветку «да», 3 в ветку «нет»; файл `~/.n8n-files/lab6/nocode_result.csv`.
2. Сразу запустить второй раз и проверить, что строки не задублировались и `updated_at` не изменился у неизменённых записей:
   ```bash
   docker exec -it lab6-postgres psql -U lab6 -d lab6 -c \
     "SELECT count(*) AS rows, count(DISTINCT event_key) AS keys, max(updated_at) AS last_change FROM events_n8n;" -c \
     "SELECT count(*) AS history_rows FROM events_n8n_history;" -c \
     "SELECT reason, count(*) FROM rejects_n8n GROUP BY 1;"
   ```
3. Включить расписание: переключатель **Active** (в n8n 2.x — **Publish**). Schedule Trigger срабатывает ежедневно в 06:47 по `GENERIC_TIMEZONE`.
4. Скопировать результат: `cp ~/.n8n-files/lab6/nocode_result.csv nocode/`.

## 5. Скриншоты (сохранить в `nocode/`)

| Файл | Что на нём |
|---|---|
| `screenshot_workflow.png` | весь холст workflow |
| `screenshot_execution.png` | успешное выполнение с числом элементов на связях (Executions → последнее) |
| `screenshot_dedup.png` | вход и выход узла «Дедупликация по event_key» (104 → 103) |
| `screenshot_if_rejects.png` | ветка «нет» узла IF и `rejects_n8n` с причинами |
| `screenshot_upsert.png` | настройки узла «Upsert в events_n8n» (Operation Upsert, match по `event_key`) |
| `screenshot_schedule.png` | Schedule Trigger с cron `47 6 * * *` и включённый Active/Publish |
| `screenshot_second_run_psql.png` | терминал с запросами из шага 4.2 после второго прогона |

## 6. Что вписать в отчёт (раздел 11)

⟪ЗАПОЛНИТЬ после прогона в n8n⟫: число элементов на каждом узле в первом прогоне (100 / 104 / 103 / 100 / 3), результаты запросов из шага 4.2 после первого и второго прогона (`rows`, `keys`, `last_change`, `history_rows`), число строк в `nocode_result.csv`.

## 7. Как workflow проверен до сдачи (на локальной имитации)

Workflow импортирован и выполнен через CLI **n8n 1.123.85 и n8n 2.35.7** (`n8n import:credentials`, `n8n import:workflow`, `n8n execute`) на копии, где URL узла HTTP Request указывал на локальную имитацию GitHub API (`tests/mock_github.py`, синтетические данные), а Postgres 16 был инициализирован `pg_init.sql`. Результаты (на локальной имитации):
- первый прогон: 100 → 104 → 103 → 100 / 3 → Upsert 100 → CSV на 100 строк; в `rejects_n8n` 3 помеченные записи с причинами;
- второй прогон: таблица `events_n8n` осталась на 100 строках, `max(updated_at)` не изменился, `events_n8n_history` — по-прежнему 100 версий, `rejects_n8n` — 3 строки.
- с недоступным API (порт закрыт): 3 попытки узла HTTP Request, затем ветка ошибки → *Stop and Error* с текстом «GitHub API недоступен после 3 попыток: connect ECONNREFUSED …».

С настоящим `api.github.com` workflow не запускался — это первый шаг реального прогона.

## 8. Ограничения n8n (честно)

- **Узел Postgres Upsert** выполняет `INSERT … ON CONFLICT (event_key) DO UPDATE SET <все поля>` **без условия WHERE**, то есть «обновляет» каждую строку при каждом запуске. Условие «обновлять только при изменении» в узле не задаётся, поэтому оно вынесено в триггер `events_n8n_upsert_guard` (`pg_init.sql`): `BEFORE UPDATE … RETURN NULL`, если бизнес-поля не изменились. Этот же триггер ведёт историю `events_n8n_history`. Проверено на PostgreSQL 16: повтор с теми же данными возвращает 0 строк, и `updated_at` не меняется.
- **Remove Duplicates** оставляет **первую** встреченную запись, а не самую свежую. Сортировку перед ним можно добавить узлом *Sort*, но правила «самая свежая по `updated_at`, затем по времени сбора» и почти-дубли в общем виде без Code не делаются.
- **Неизменяемый сырой слой, контракт pydantic, метрики со «светофором»** и прерывание по красному статусу в n8n не реализованы. *Read/Write Files* с опцией *Append* умеет дописывать файл, но не контролирует, что старые строки не менялись.
- **Если Docker недоступен:** в n8n ≥ 1.113 есть встроенные *Data tables* с операцией *Upsert*. Узлы Postgres можно заменить узлом *Data table* (Upsert по `event_key`), но тогда пропадут CHECK-ограничения и триггер-история.
