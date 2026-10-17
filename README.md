# Инструменты автоматизированного сбора данных в сети Интернет

Лабораторные работы и итоговый проект по курсу сбора и парсинга данных.
Студент: Подольский Ф. А., ПМИ-3-2025. Вариант во всех лабораторных — **5** (№ 22 в списке группы, `(22 mod 6) + 1`).

## Структура репозитория

```
solutions/
├── lab1/                      REST API: GitHub (репозитории пользователя) и NASA (запросы с ключом)
├── lab2/                      BeautifulSoup + Octoparse: учебный интернет-магазин webscraper.io
├── lab3_podolskiy_5/          REST API: CoinGecko /coins/markets + /market_chart, n8n
├── lab4_podolskiy_5/          динамические страницы: quotes.toscrape.com/login, Playwright, n8n
├── lab5_podolskiy_5/          промышленный краулинг: Scrapy-проект + n8n
├── lab6_podolskiy_5/          хранение и качество: raw → clean → mart, GitHub API + HTML, SQLite, pydantic
├── lab7_podolskiy_5/          устойчивый пайплайн: token bucket, повторы, предохранитель, ETag, логи
└── final_project_podolskiy/   итоговый проект: GitHub Topics (HTML) + GitHub REST API
run_all.sh                     реальный прогон всех лаб, которым нужны учебные сайты
start_n8n.sh                   запуск n8n для no-code частей
```

Материалы курса (задания, блокноты занятий) лежат локально в `materials/` и в репозиторий не публикуются.

## Работы

| Работа | Источник (вариант 5) | Состояние |
|---|---|---|
| [Лаб. 1 — REST API](./solutions/lab1/README.md) | GitHub API, NASA Open APIs | готово, данные собраны |
| [Лаб. 2 — BeautifulSoup и Octoparse](./solutions/lab2/README.md) | webscraper.io (учебный магазин) | готово, данные собраны |
| [Лаб. 3 — сбор через REST API](./solutions/lab3_podolskiy_5/README.md) | CoinGecko: `/coins/markets`, `/coins/{id}/market_chart` | готово: реальный прогон и n8n, 16.10.2026 |
| [Лаб. 4 — динамические страницы](./solutions/lab4_podolskiy_5/README.md) | quotes.toscrape.com/login: CSRF, `storage_state` | готово: Playwright, requests и n8n, 16.10.2026 |
| [Лаб. 5 — Scrapy и n8n](./solutions/lab5_podolskiy_5/README.md) | scrapingcourse.com/ecommerce (закрыт robots.txt) → согласованная замена scrapeme.live/shop | готово: Scrapy и n8n, 16.10.2026 |
| [Лаб. 6 — хранение и качество](./solutions/lab6_podolskiy_5/README.md) | GitHub API (репозитории, issues) + HTML страниц репозиториев | готово: реальные данные 15.10.2026, n8n → Postgres 16.10.2026 |
| [Лаб. 7 — устойчивый пайплайн](./solutions/lab7_podolskiy_5/README.md) | Scrapy-краулер лаб. 5 (scrapeme.live) + httpbin.org | готово: реальный прогон и n8n, 16.10.2026 |
| [Итоговый проект](./solutions/final_project_podolskiy/README.md) | github.com/topics (HTML) + GitHub REST API | готово: реальные данные 15.10.2026, запуски из n8n 16.10.2026 |

Каждая работа самодостаточна: свой `README.md` (воспроизведение с нуля и таблица «требование → где выполнено»),
`report.md`, `requirements.txt`, `run_real.sh`. Цифры в отчётах взяты только из измерений реальных прогонов.

## Запуск

```bash
./run_all.sh                  # лабы 3, 4, 5, 7 — около 40–50 минут, у каждой свой .venv и журнал logs/real_run_*.log
./run_all.sh 3                # одна лаба
./start_n8n.sh                # n8n на http://localhost:5678 (нужен Node.js >= 24)
```

Лаба 6 и итоговый проект уже прогнаны на реальном GitHub; повтор — `./run_all.sh --with-github`
(нужен `GITHUB_TOKEN` в `.env` соответствующей папки, файл `.env` в Git не попадает).

## Источник лабораторных 5 и 7

`https://www.scrapingcourse.com/robots.txt` на 15.10.2026 содержит `User-agent: *` / `Disallow: /ecommerce/*`.
Задание требует `ROBOTSTXT_OBEY = True`, поэтому краулер останавливается на проверке robots.txt (это зафиксировано
как доказательство). По согласованию с преподавателем использована замена — `https://scrapeme.live/shop/` (тот же
тип WooCommerce-каталога, 755 товаров, `/shop/` разрешён robots.txt):
`START_URL=https://scrapeme.live/shop/ ./run_all.sh 5 7`. Текст запроса на согласование — в
`solutions/lab5_podolskiy_5/README.md`.
