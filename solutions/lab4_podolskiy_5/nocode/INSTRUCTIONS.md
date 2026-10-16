# No-code часть (n8n): вход с CSRF и пагинация после авторизации

> **Как запускать на этом Mac:** n8n запускается одной командой из корня папки курса — `./start_n8n.sh` (в ней уже заданы все переменные окружения). Результаты workflow пишутся в `~/.n8n-files/lab4/`, оттуда их нужно скопировать в `nocode/` этой лабы. Общая пошаговая инструкция — `ИНСТРУКЦИЯ.md` в корне.


**Сценарий (упрощённый, тот же источник):** `GET /login` → извлечь hidden `csrf_token` (узел HTML) →
`POST /login` (form-urlencoded) с cookie сессии → проверить редирект 3xx → склеить cookie →
лента `/page/N/` с той же cookie через встроенную пагинацию HTTP Request → разбор карточек →
нормализация → удаление дублей → smoke-check → `nocode/nocode_result.csv`.

**Почему без рендеринг-API.** Диагностика (`diagnose.py`, `metrics/diagnostics.json`) показывает, что страницы
`quotes.toscrape.com` формируются сервером, XHR на целевых страницах нет, а вход и пагинация работают с
отключённым JS. Поэтому узел HTTP Request получает готовый HTML и **ни одного запроса к платному
рендеринг-API не нужно** (0 запросов с JS-рендерингом за прогон). Ограничение n8n — у HTTP Request нет
общей «банки» cookie между узлами, поэтому cookie сессии передаётся вручную заголовком `Cookie`
(узлы «POST /login» и «Cookie сессии»).

## Состав workflow (`workflow.json`, формат экспорта n8n)

| # | Узел | Тип | Настройка |
|---|------|-----|-----------|
| 1 | Ручной запуск / Расписание: ежедневно 07:10 | Manual Trigger, Schedule Trigger | два входа в один сценарий |
| 2 | Config | Edit Fields (Set) | `baseUrl`, учебные `username`/`password` (любые), `userAgent`, `outputPath` |
| 3 | GET /login | HTTP Request 4.2 | ответ «Include Response Headers and Status», формат Text; Retry On Fail: 3 попытки через 2000 мс; On Error → ветка ошибки |
| 4 | HTML: csrf_token | HTML → Extract HTML Content | `form input[name="csrf_token"]` → атрибут `value` |
| 5 | POST /login | HTTP Request 4.2 | Form URL Encoded: `csrf_token`, `username`, `password`; заголовок `Cookie` из `Set-Cookie` шага 3; **Follow Redirects = off**, Never Error = on |
| 6 | Вход выполнен? | If | `statusCode` в диапазоне 300–399; иначе → «Вход не выполнен» (Stop and Error) |
| 7 | Cookie сессии | Code | склейка cookie из ответов GET и POST (сервер выдаёт новую cookie после входа) |
| 8 | GET /page/N (пагинация после входа) | HTTP Request 4.2 | Pagination: *Response Contains Next URL* (ссылка из `<li class="next">`), завершение — нет `li.next`; **Max Pages = 15**, интервал **1000 мс**; Retry 3×2000 мс; On Error → ветка ошибки |
| 9 | HTML: карточки цитат | HTML | `[itemtype*="schema.org/CreativeWork"]` → HTML (массив), признак входа `a[href="/logout"]` |
| 10 | Split Out: по цитате | Split Out | поле `quotes`, + `loggedIn` |
| 11 | HTML: поля цитаты | HTML | `[itemprop="text"]`, `[itemprop="author"]`, `a[href*="/author/"]`, `a[href*="goodreads.com"]`, `meta[itemprop="keywords"]@content` |
| 12 | Нормализация | Code | пробелы, кавычки, абсолютные URL, `tags_all`, `n_tags`, `auth_ok`, `collected_at` |
| 13 | Удалить дубликаты | Remove Duplicates | по `text, author` |
| 14 | Smoke-check | Code | ≥ 20 строк и ≤ 5 % пустых `text/author/author_url/goodreads_url/auth_ok`, иначе ошибка |
| 15 | CSV → Записать nocode_result.csv | Convert to File (CSV) → Read/Write Files from Disk | путь `outputPath` |
| — | Детали сбоя → Остановить: сбой запроса | Set → Stop and Error | ветка ошибки узлов 3, 5, 8 |

**Проверено до сдачи** (на локальной имитации сайта, не на реальном): `workflow.json` импортирован через CLI в
n8n 2.43.6 (Node.js 24) и выполнен `n8n execute`: вход 302 → 10 страниц → 100 строк в `nocode_result.csv`,
совпадение с лентой браузерного сбора по `quote_id`, `goodreads_url`, тегам — 100 из 100 (на локальной имитации).
Ветка ошибки проверена недоступным хостом: «GET /login» → повторы → «Детали сбоя» → «Остановить: сбой запроса».
Найденная при проверке ловушка: узел HTML (v1.2) извлекает текст через html-to-text с переносом строк по
80 символов, а опция *Clean Up Text* удаляет переносы **без пробела** и склеивает слова — поэтому в узлах HTML она
выключена, пробелы нормализует узел «Нормализация». Не включайте её обратно.

## Запуск (macOS)

1. Node.js: актуальный n8n 2.4x требует **Node.js ≥ 24** (`node -v`; установка — `brew install node@24` или nvm).
2. Запустить n8n **из папки лабораторной** и разрешить запись в `nocode/` (в n8n ≥ 1.9x доступ к диску
   ограничен переменной `N8N_RESTRICT_FILE_ACCESS_TO`, по умолчанию — только `~/.n8n-files`):
   ```bash
   cd lab4_podolskiy_5
   export N8N_RESTRICT_FILE_ACCESS_TO="$PWD/nocode"
   npx n8n            # первый запуск скачивает n8n (несколько минут); UI: http://localhost:5678
   ```
   `outputPath` в узле Config — `{{ $env.HOME }}/.n8n-files/lab4/nocode_result.csv` (при запуске через `./start_n8n.sh`).
   Если запускаете из другой папки — укажите в Config абсолютный путь внутри разрешённой папки.
3. Импорт: *Workflows → Create → ⋯ → Import from File…* → `nocode/workflow.json`
   (или в терминале: `npx n8n import:workflow --input=nocode/workflow.json`).
4. Запуск: открыть workflow → **Execute workflow** (срабатывает «Ручной запуск»).
   Без UI (для повторных замеров): `npx n8n execute --id=Lab4Podolskiy5Wf`.
   Для регулярного сбора — переключатель **Active** (сработает «Расписание: ежедневно 07:10»).
5. Скопировать и проверить файл: `cp ~/.n8n-files/lab4/nocode_result.csv nocode/ && wc -l nocode/nocode_result.csv` (ожидается 100 строк данных + заголовок, если на сайте 10 страниц ленты).

## Что снять и куда записать

Скриншоты сохранить в `nocode/` ровно с такими именами:

| Файл | Что на нём |
|------|------------|
| `screenshot_workflow.png` | холст workflow после успешного прогона: все узлы основной ветки с зелёными галочками и числом items |
| `screenshot_execution.png` | вкладка **Executions**: строка прогона со статусом *Succeeded* и длительностью |
| `screenshot_login.png` | узел «POST /login» → Output: `statusCode` 302 и `set-cookie` (значение cookie можно замазать) |
| `screenshot_result.png` | узел «Smoke-check» → Output в режиме Table: строки с `text`, `author`, `goodreads_url` |
| `screenshot_file.png` | терминал: `wc -l nocode/nocode_result.csv` и `head -3 nocode/nocode_result.csv` |

Замеры для таблицы сравнения (report.md, раздел 6; `summarize.py` подставит число строк сам):

| Метрика | Где взять |
|---------|-----------|
| Время сборки workflow, мин | собственный учёт: импорт + настройка/проверка узлов (или сборка с нуля по таблице выше) |
| Время одного прогона, с | Executions → длительность прогона (среднее по 3 запускам) |
| Записей | `wc -l nocode/nocode_result.csv` − 1 |
| Число запросов | 1 (GET /login) + 1 (POST /login) + число страниц ленты (в узле 8 — число output items) |
| Запросов с JS-рендерингом | 0 (рендеринг не нужен, см. диагностику) |
| Пиковая память | «Мониторинг системы» → процесс `node` (n8n) во время прогона |

**Масштабирование стоимости.** Один прогон = 2 + P запросов (P — страниц ленты, по 10 цитат). Для 10 000
записей в день это ≈ 1 000 страниц/день ≈ 1 002 обычных HTTP-запроса — бесплатно на своём n8n. Если бы
сайт требовал JS-рендеринга, каждая страница стала бы платным запросом к рендеринг-API
(ScrapingBee / ScraperAPI / Browserless): стоимость = P × цена рендер-запроса по тарифу провайдера,
то есть ≈ 30 000 рендер-запросов в месяц для того же объёма.

## Ограничения n8n в этом сценарии

- Нет общей cookie-сессии между узлами: cookie переносится вручную; при смене имени/формата cookie
  (например, несколько cookie или `Secure; SameSite`) узел «Cookie сессии» справляется, но перенос через
  заголовок — ответственность автора workflow.
- Следующая страница ищется регулярным выражением по HTML (`<li class="next">…href`): чувствительно к
  переименованию класса пагинации — это слабее, чем `get_by_role("link", name="Next")` в Playwright.
- Нет браузера: если вход станет JS-формой или появится капча — нужен рендеринг-API (платно) или Octoparse;
  обход капчи в рамках курса запрещён.
- Предохранители: Max Pages = 15, интервал 1000 мс, Retry 3 × 2000 мс, ветка ошибки → Stop and Error,
  Smoke-check останавливает прогон при пустых полях.

## Запасной вариант: Octoparse (если n8n недоступен)

1. *New → Custom Task*, URL `https://quotes.toscrape.com/login`.
2. Клик по полю **Username** → *Enter text* → `student`; клик по полю **Password** → *Enter text* → `student`
   (CSRF-токен браузер Octoparse отправит сам — он в hidden-поле формы).
3. Клик по кнопке **Login** → *Click button*; в настройках шага: *Wait before action* 2 с, *AJAX* выкл.
4. Шаг *Go to Web Page* `https://quotes.toscrape.com/page/1/` в той же задаче: встроенный браузер Octoparse
   хранит cookie сессии между шагами (концептуальный аналог `storage_state`). Проверка — в превью видна ссылка
   «Logout» и «(Goodreads page)» у цитат.
5. Выделить первую карточку цитаты, затем вторую → *Select all* → *Extract data* (*Loop Item*).
   Поля: текст цитаты, автор, ссылка «(about)» (*Extract URL*), ссылка «(Goodreads page)» (*Extract URL*),
   теги. Переименовать столбцы: `text, author, author_url, goodreads_url, tags_all`.
6. Пагинация: кликнуть по **Next →** → *Loop click next page*; *Wait before action* 1–2 с; лимит повторов 15.
7. *Save → Run → Run on your device*; экспорт CSV → сохранить как `nocode/nocode_result.csv`.
8. Скриншоты: `screenshot_workflow.png` (workflow с шагами входа), `screenshot_result.png` (таблица данных),
   `screenshot_file.png` (экспортированный CSV). Экспорт задачи (*Export task* → `.otd`) положить в `nocode/`
   и указать в отчёте, что `workflow.json` — для n8n, а `.otd` — для Octoparse.
