# n8n: импорт, запуск, скриншоты (лабораторная 5)

Workflow повторяет конвейер Scrapy на подмножестве: 2 страницы каталога × 16 карточек = 32 карточки
(после проверки ≥ 30 записей). Проверен в n8n 2.43.6 (запуск через CLI на локальной имитации магазина):
30 записей в CSV, ветка ошибки пишет журнал.

**Запускать только на источнике, который разрешён robots.txt.** n8n сам robots.txt не читает — это
ответственность автора workflow. Сначала `python3 tools/robots_preflight.py --start-url <URL>`;
если вердикт «запрещён» (так сейчас у scrapingcourse.com/ecommerce), меняйте `base_url` только на
согласованный с преподавателем источник (например, `https://scrapeme.live/shop/`).

## 1. Запуск n8n

```bash
mkdir -p ~/.n8n-files                              # сюда workflow пишет CSV и журнал ошибок
export N8N_RESTRICT_FILE_ACCESS_TO="$HOME/.n8n-files"   # n8n ≥ 1.9x: доступ к диску только к этой папке
export N8N_BLOCK_ENV_ACCESS_IN_NODE=false          # n8n 2.x по умолчанию запрещает $env; путь к файлу строится из $env.HOME
npx n8n                                            # UI: http://localhost:5678 (n8n 2.x требует Node.js ≥ 24)
```

Если включать доступ к `$env` не хочется — в узлах «Записать nocode_result.csv» и «Дописать n8n_errors.log»
замените выражение `{{ $env.HOME }}/.n8n-files/...` на абсолютный путь, например `/Users/<имя>/.n8n-files/nocode_result.csv`.

## 2. Импорт

UI → **Workflows → Create → ⋯ → Import from File…** → `nocode/workflow.json`.
В узле **«Параметры»** проверьте `base_url` (по умолчанию источник варианта) и `pages = 2`.

## 3. Устройство

| Узел | Тип | Аналог в Scrapy |
|---|---|---|
| Расписание: ежедневно 05:00 / Ручной запуск | Schedule Trigger (cron `0 5 * * *`) / Manual Trigger | cron / Scrapyd |
| Параметры | Set | `start_url`, `max_pages`, `USER_AGENT` |
| Номера страниц | Code — цикл по номерам `/page/N/` | пагинация |
| Страница каталога | HTTP Request: batching 1 запрос / 1,5 с, таймаут 20 с, **Retry On Fail 3×3 с**, выход ошибки | Downloader + `DOWNLOAD_DELAY` + RetryMiddleware |
| Ссылки на карточки | HTML Extract (`a.woocommerce-LoopProduct-link`, массив) | `parse()` |
| По одной ссылке | Split Out | генератор запросов |
| Убрать повторы URL | Remove Duplicates (по URL) | dupefilter |
| Батчи по 4 карточки → Карточка товара → Поля карточки → Пауза 1 с | Loop Over Items + HTTP Request (2 параллельно, Retry On Fail) + HTML Extract + Wait | `parse_item`, `CONCURRENT_REQUESTS_PER_DOMAIN=2`, `DOWNLOAD_DELAY` |
| Убрать дубли SKU | Remove Duplicates (по `sku`) | `DedupPipeline` |
| Проверка и типизация | Code: валидация, цена → строка с 2 знаками, валюта, категории | `ValidationPipeline` + `CleanPipeline` |
| В CSV → Записать nocode_result.csv | Convert to File (CSV) + Read/Write Files from Disk | `SQLitePipeline` / Feed Exports |
| Ошибка страницы / Ошибка карточки → Текст уведомления → журнал `n8n_errors.log` + Telegram | выходы ошибок HTTP-узлов, Set, Code, Convert to File (append), Telegram (**отключён** до настройки бота) | `errback` + оповещения |

## 4. Выполнение и замеры

1. Нажмите **Execute workflow** (ручной запуск). Засеките время сборки workflow — это «время разработки»
   (вписать в `n8n_metrics.json`).
2. После выполнения: **Executions** → последняя запись → длительность (секунды) — это «время прогона».
3. Скопируйте результат в лабораторную: `cp ~/.n8n-files/nocode_result.csv nocode/nocode_result.csv`.
4. `cp nocode/n8n_metrics.example.json nocode/n8n_metrics.json` и впишите: `n8n_dev_minutes`, `scrapy_dev_minutes`,
   `run_seconds` (из Executions), `http_requests` = 2 + число карточек (по умолчанию 34), оценки поддержки 1–5.
5. `python3 tools/compare_table.py` — таблица сравнения в `nocode/comparison_table.md` (её вставить в `report.md`).

Проверка ветки ошибки (по желанию, для скриншота): в «Параметрах» временно поставьте
`base_url = http://127.0.0.1:9/ecommerce/` (закрытый порт) и `pages = 1`, выполните — узел «Страница каталога»
после 3 попыток уйдёт в выход ошибки, в `~/.n8n-files/n8n_errors.log` появится строка. Верните `base_url`.

## 5. Скриншоты (положить в `nocode/`)

| Файл | Что на нём |
|---|---|
| `screenshot_workflow.png` | вся схема workflow (кнопка «Zoom to fit») |
| `screenshot_execution.png` | успешное выполнение: зелёные узлы с числом элементов на связях (32 ссылки → 30 записей) |
| `screenshot_executions_list.png` | список Executions с длительностью выполнения |
| `screenshot_result_csv.png` | `nocode_result.csv`, открытый в редакторе/таблице (видно ≥ 30 строк) |
| `screenshot_node_settings.png` | настройки узла «Карточка товара»: Retry On Fail, Max Tries, Wait Between Tries, On Error → Continue (using error output) |
| `screenshot_error_branch.png` | (по желанию) выполнение с ошибкой: выход ошибки и строка в `n8n_errors.log` |

## 6. Что вписать в таблицу сравнения (report.md, раздел 10)

Записей собрано (строк CSV), время прогона (Executions), записей в минуту (считает `compare_table.py`),
число запросов (34), трафик — n8n его не показывает, поэтому в таблице оценка «запросы × средний размер
ответа из прогона Scrapy на тех же страницах» с пометкой «оценка».
