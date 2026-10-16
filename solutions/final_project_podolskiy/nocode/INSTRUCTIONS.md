# n8n: импорт, запуск, скриншоты

Workflow `nocode/workflow.json` оркестрирует пайплайн: расписание → запуск `src/run_pipeline.py` → разбор
`RUN_SUMMARY` → оповещение при жёлтом/красном статусе → Stop and Error при красном; ветка Error Trigger.
Сравнение «код / no-code» — `nocode/notes.md`.

## 1. Подготовка (macOS, один раз)

Сначала выполните реальный прогон (`./run_real.sh`) — он создаёт `.venv` и данные, на которые опирается workflow.

```bash
mkdir -p ~/.n8n-files/podolskiy                      # сюда n8n пишет файлы оповещений
export N8N_RESTRICT_FILE_ACCESS_TO="$HOME/.n8n-files" # узлы Read/Write Files работают только в этом каталоге
export NODES_EXCLUDE="[]"                             # n8n >= 2.0: включить узел Execute Command (по умолчанию отключён)
export GENERIC_TIMEZONE="UTC"                         # часовой пояс расписания по умолчанию
npx n8n                                               # UI: http://localhost:5678
```

Версии (проверено по реестру npm 15.10.2026): актуальная n8n 2.43.6 требует **Node.js ≥ 24** (`node -v`); при
Node.js 20.19–24 используйте ветку 1.x: `npx n8n@1`. В n8n 2.x узел Execute Command исключён по умолчанию
(`NODES_EXCLUDE = ["n8n-nodes-base.executeCommand", "n8n-nodes-base.localFileTrigger"]` в конфигурации n8n) —
поэтому `NODES_EXCLUDE="[]"` обязателен; в 1.x переменная не нужна, но не мешает. Запись файлов по умолчанию
разрешена только в `~/.n8n-files`. Ваша версия: `npx n8n --version` → ⟪ЗАПОЛНИТЬ⟫.
Если узел Execute Command в вашей версии недоступен вовсе — см. раздел 5 «Запасной вариант».

## 2. Импорт и настройка

1. **Workflows → Import from File** → `nocode/workflow.json`
   (или из терминала: `npx n8n import:workflow --input=nocode/workflow.json`).
2. Узел **Параметры запуска**: замените `YOUR_USER` в `project_dir` и `alerts_dir` на имя пользователя
   (`echo $HOME` покажет путь). `python` оставьте `.venv/bin/python`, `args` и `env_prefix` — пустыми.
3. **Settings (⋯ → Settings) → Error workflow** → выберите этот же workflow (ветка «При сбое workflow»).
   Ветка Error Trigger срабатывает для запусков по расписанию (не для ручных). Часовой пояс workflow уже задан: UTC.
4. Telegram (необязательно): создайте бота у @BotFather, добавьте учётные данные Telegram API в узел
   «Telegram (включить при наличии бота)», впишите `chatId`, включите узел (Deactivate → Activate node).
5. Сохраните. Для работы по расписанию переключите workflow в **Active** (в n8n 2.x — **Publish**).

## 3. Запуски для отчёта и защиты

| Запуск | Что задать в «Параметры запуска» | Ожидаемый путь в n8n |
|---|---|---|
| Штатный ручной | ничего (или `args = --topics web-scraping --max-pages 1` для скорости) | Разбор RUN_SUMMARY → «Нужно оповещение?» = false → «Прогон здоров» (если вердикт green) |
| Жёлтый/красный по данным | обычный запуск после демо-сценариев (серия без изменений) | «Текст оповещения» → файл `~/.n8n-files/podolskiy/alert_*.json` |
| Отказ источника | `env_prefix = GITHUB_API_BASE=https://api.github.invalid`, `args = --topics web-scraping --max-pages 1 --refresh-max-age-hours 0` | код 3 → оповещение → файл → «Красный статус?» → **Stop and Error** (выполнение помечено ошибкой) |
| Сбой запуска | `python = .venv/bin/python-missing` | ветка ошибки Execute Command → «Сбой запуска команды» → оповещение |

После демонстрации верните `env_prefix` и `python` к исходным значениям.

## 4. Скриншоты (сохранить в `nocode/screenshots/`)

| Файл | Что на нём |
|---|---|
| `screenshot_workflow.png` | холст целиком: все узлы и связи, заметка с настройкой |
| `screenshot_schedule.png` | открытый узел «Расписание 03:17 UTC» (cron `17 3 * * *`) |
| `screenshot_execution_ok.png` | успешное выполнение: зелёный путь, вывод узла «Разбор RUN_SUMMARY» (verdict, run_id, счётчики) |
| `screenshot_execution_failure.png` | выполнение со сценарием «Отказ источника»: путь через оповещение до Stop and Error, код 3 |
| `screenshot_alert_file.png` | содержимое `~/.n8n-files/podolskiy/alert_*.json` (Finder/терминал: `cat ~/.n8n-files/podolskiy/alert_*.json`) |
| `screenshot_executions_list.png` | вкладка Executions: несколько выполнений с разным статусом |

## 5. Запасной вариант расписания (если Execute Command недоступен)

Расписание и запуск — системным cron (время — локальное, 08:17 по Перми = 03:17 UTC):

```bash
crontab -e
17 8 * * * cd "$HOME/final_project_podolskiy" && .venv/bin/python src/run_pipeline.py --quiet >> logs/cron.log 2>&1
```

Оповещения в этом случае — по `reports/latest_run_report.json` (поле `verdict`) и коду возврата в `logs/cron.log`.
В отчёте нужно честно указать, что no-code-часть сведена к оповещениям, и почему (политика безопасности n8n 2.x).

## 6. Что вписать в таблицу сравнения

`nocode/notes.md` уже содержит таблицу «этап → код / no-code → что не удалось перенести». После запуска дополните
строку «HTTP-запросов пайплайна за прогон» (из `reports/latest_run_report.json → requests.total`) и, если узлы
вели себя иначе, чем описано (версия n8n, названия меню), — отметьте это там же.
