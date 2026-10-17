# No-code часть: n8n → Postgres (Upsert), отбраковка, дедупликация, расписание

> Короткий путь: `./start_n8n.sh` из корня репозитория запускает n8n с нужными переменными окружения; workflow
> пишет результат в `~/.n8n-files/lab6/`, откуда он копируется в `nocode/`.

Файлы: `workflow.json` (экспорт n8n), `pg_init.sql` (таблицы и триггер), `docker-compose.yml` (Postgres 16).
Результат после прогона: `nocode_result.csv` и скриншоты `screenshot_*.png` в этой папке.

## Что делает workflow

```
Каждый день 06:47 ─┐
Запуск вручную ────┴─► GitHub API: issues scrapy/scrapy ──(ошибка после 5 попыток)──► Ошибка HTTP (Stop and Error)
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

1. **Credentials → Add credential → Postgres**, имя `Postgres lab6 (docker)`: Host `127.0.0.1` (не `localhost`: n8n на Node.js 24 разрешает его в IPv6 `::1`, а Docker публикует порт на IPv4 — будет `ECONNREFUSED ::1:5432`), Port `5432`, Database `lab6`, User `lab6`, Password `lab6_local_only`, SSL `disable`. Нажать **Test** → *Connection tested successfully*.
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

## 5. Скриншоты (`nocode/`)

| Файл | Что на нём |
|---|---|
| `screenshot_workflow.png` | весь холст workflow |
| `screenshot_execution.png` | успешное выполнение с числом элементов на связях (Executions → последнее) |
| `screenshot_dedup.png` | вход и выход узла «Дедупликация по event_key» (104 → 103) |
| `screenshot_if_rejects.png` | ветка «нет» узла IF и `rejects_n8n` с причинами |
| `screenshot_upsert.png` | настройки узла «Upsert в events_n8n» (Operation Upsert, match по `event_key`) |
| `screenshot_schedule.png` | Schedule Trigger с cron `47 6 * * *` (workflow не публиковался) |
| `screenshot_second_run_psql.png` | терминал с запросами из шага 4.2 после второго прогона |

## 6. Результат реального прогона (16.10.2026, раздел 11 отчёта)

Реальный прогон 16.10.2026 (n8n 2.35.7, настоящий `api.github.com`, Postgres 16 в Docker), запуски № 28 и № 30 в Executions:

| Узел | Первый прогон | Второй прогон |
|---|---|---|
| GitHub API: issues scrapy/scrapy | 100 (1,2 с) | 100 |
| Демо: синтетические битые записи | 104 | 104 |
| Дедупликация по event_key | 103 | 103 |
| Запись валидна? (да / нет) | 100 / 3 | 100 / 3 |
| Upsert в events_n8n | 100 | 1 пустой item — триггер отбросил все неизменённые строки |
| Upsert в rejects_n8n | 3 | 3 |
| Длительность | 1,5 с | 1,5 с |

Запросы шага 4.2 после первого и после второго прогона дали одинаковый результат: `rows` 100, `keys` 100, `last_change` 2026-10-16 13:28:24 UTC, `history_rows` 100; `rejects_n8n` — по одной строке на причину («state=closed без closed_at», «пустой title», «comments < 0 или пусто»). `nocode_result.csv` — 100 строк.

Самый первый запуск (№ 27) упал на узле Postgres с `ECONNREFUSED ::1:5432` — после замены Host на `127.0.0.1` ошибка ушла. Retry On Fail узла HTTP Request поднят с 3 до 5 попыток через 5 с (максимум n8n): в используемой сети часть новых TLS-соединений обрывается (замеры — в отчёте лабы 4). Расписание настроено (cron `47 6 * * *`), но workflow не публиковался: для отчёта достаточно настроенного узла.

## 7. Предварительная проверка на локальной имитации

Workflow импортирован и выполнен через CLI **n8n 1.123.85 и n8n 2.35.7** (`n8n import:credentials`, `n8n import:workflow`, `n8n execute`) на копии, где URL узла HTTP Request указывал на локальную имитацию GitHub API (`tests/mock_github.py`, синтетические данные), а Postgres 16 был инициализирован `pg_init.sql`. Результаты (на локальной имитации):
- первый прогон: 100 → 104 → 103 → 100 / 3 → Upsert 100 → CSV на 100 строк; в `rejects_n8n` 3 помеченные записи с причинами;
- второй прогон: таблица `events_n8n` осталась на 100 строках, `max(updated_at)` не изменился, `events_n8n_history` — по-прежнему 100 версий, `rejects_n8n` — 3 строки.
- с недоступным API (порт закрыт): 3 попытки узла HTTP Request (в той версии workflow; сейчас их 5), затем ветка ошибки → *Stop and Error* с текстом «GitHub API недоступен после N попыток: connect ECONNREFUSED …».

С настоящим `api.github.com` workflow выполнен 16.10.2026 — результаты в разделе 6.

## 8. Ограничения n8n

- **Узел Postgres Upsert** выполняет `INSERT … ON CONFLICT (event_key) DO UPDATE SET <все поля>` **без условия WHERE**, то есть «обновляет» каждую строку при каждом запуске. Условие «обновлять только при изменении» в узле не задаётся, поэтому оно вынесено в триггер `events_n8n_upsert_guard` (`pg_init.sql`): `BEFORE UPDATE … RETURN NULL`, если бизнес-поля не изменились. Этот же триггер ведёт историю `events_n8n_history`. Проверено на PostgreSQL 16: повтор с теми же данными возвращает 0 строк, и `updated_at` не меняется.
- **Remove Duplicates** оставляет **первую** встреченную запись, а не самую свежую. Сортировку перед ним можно добавить узлом *Sort*, но правила «самая свежая по `updated_at`, затем по времени сбора» и почти-дубли в общем виде без Code не делаются.
- **Неизменяемый сырой слой, контракт pydantic, метрики со «светофором»** и прерывание по красному статусу в n8n не реализованы. *Read/Write Files* с опцией *Append* умеет дописывать файл, но не контролирует, что старые строки не менялись.
- **Если Docker недоступен:** в n8n ≥ 1.113 есть встроенные *Data tables* с операцией *Upsert*. Узлы Postgres можно заменить узлом *Data table* (Upsert по `event_key`), но тогда пропадут CHECK-ограничения и триггер-история.
