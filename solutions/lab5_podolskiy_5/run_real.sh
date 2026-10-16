#!/usr/bin/env bash
# Реальный прогон лабораторной № 5 (вариант 5) на macOS / Linux.
#
#   ./run_real.sh                                         # источник варианта: scrapingcourse.com/ecommerce
#   START_URL=https://scrapeme.live/shop/ ./run_real.sh   # ТОЛЬКО после согласования замены источника
#
# Порядок: venv → проверка robots.txt → (если обход разрешён) итоговый прогон →
# демонстрация JOBDIR/HTTPCACHE → прогон-подмножество для сравнения с n8n →
# smoke-check → исполнение блокнота качества. Весь вывод — в logs/real_run_<время>.log.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs data
LOG="logs/real_run_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

START_URL="${START_URL:-https://www.scrapingcourse.com/ecommerce/}"
PY="${PYTHON:-python3}"
echo "== Лабораторная 5: старт $(date '+%F %T'), источник: $START_URL"

# 1. Виртуальное окружение
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
python -c "import scrapy, sys; print('Python', sys.version.split()[0], '| Scrapy', scrapy.__version__)"

# 2. robots.txt: решение принимается до сбора (тот же парсер Protego, что в Scrapy)
set +e
python tools/robots_preflight.py --start-url "$START_URL"
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  echo "== robots.txt запрещает обход ($START_URL) или недоступен (код $RC)."
  echo "== Фиксируем доказательство: паук с ROBOTSTXT_OBEY=True делает 1 запрос (robots.txt),"
  echo "== все страницы каталога отсекает (robotstxt/forbidden), записей 0."
  scrapy crawl ecommerce -a start_url="$START_URL" -a run_label=robots_evidence \
      -s CLOSESPIDER_TIMEOUT=120 -s SQLITE_PATH=data/demo/robots_evidence.db -s 'FEEDS={"data/demo/items_robots_evidence.jsonl": {"format": "jsonlines", "overwrite": true}}'
  echo "== СБОР ОСТАНОВЛЕН. Нужна замена источника по согласованию с преподавателем"
  echo "== (см. README, раздел «Источник закрыт robots.txt»). После согласования:"
  echo "==   START_URL=https://scrapeme.live/shop/ ./run_real.sh"
  echo "== Журнал: $LOG"
  exit 0
fi

# 3. Проверка селекторов на одной странице (кэш включён: повторная отладка не нагружает сайт)
scrapy crawl ecommerce -a start_url="$START_URL" -a max_pages=1 -a run_label=selectors_check \
    -s HTTPCACHE_ENABLED=True -s SQLITE_PATH=data/demo/selectors.db \
    -s 'FEEDS={"data/demo/items_selectors_check.jsonl": {"format": "jsonlines", "overwrite": true}}'
python - <<'EOF'
import json
rows = [json.loads(l) for l in open("data/demo/items_selectors_check.jsonl", encoding="utf-8") if l.strip()]
fields = ["sku", "name", "price", "categories", "description", "image_url"]
print(f"Проверка селекторов: {len(rows)} карточек с первой страницы")
for f in fields:
    print(f"  {f:12s} заполнено {sum(1 for r in rows if r.get(f))}/{len(rows)}")
if not rows:
    raise SystemExit("Ни одной записи: разметка отличается от ожидаемой, см. README → «Если селекторы не сработали»")
EOF

# 4. Итоговый прогон: свежая БД, предохранитель CLOSESPIDER_ITEMCOUNT=400 (settings.py)
rm -f data/crawl.db
START=$(date +%s)
scrapy crawl ecommerce -a start_url="$START_URL" -a run_label=main
echo "== Итоговый прогон: $(( $(date +%s) - START )) с (по часам оболочки)"

# 5. Демонстрация JOBDIR и HTTPCACHE (подмножество: 4 и 3 страницы каталога)
python tools/demo_resume_cache.py --start-url "$START_URL"

# 6. Подмножество для сравнения с n8n: те же 2 страницы, что обрабатывает workflow
scrapy crawl ecommerce -a start_url="$START_URL" -a max_pages=2 -a run_label=subset_n8n \
    -s SQLITE_PATH=data/demo/subset.db \
    -s 'FEEDS={"data/demo/items_subset_n8n.jsonl": {"format": "jsonlines", "overwrite": true}}'

# 7. Smoke-check (норма/предупреждение) и проверка, что тревога срабатывает
python quality/smoke_check.py || echo "== smoke-check выдал предупреждение (см. выше)"
python quality/smoke_check.py --self-test

# 8. Блокнот качества: исполняется на месте, с выводами
python -m jupyter nbconvert --to notebook --execute --inplace \
    --ExecutePreprocessor.timeout=600 quality/quality_report.ipynb

# 9. Таблица сравнения Scrapy vs n8n (заготовка; время n8n вписывается после запуска workflow)
python tools/compare_table.py

echo "== Готово $(date '+%F %T'). Журнал: $LOG"
echo "== Дальше вручную: n8n по nocode/INSTRUCTIONS.md, затем заполнить ⟪ЗАПОЛНИТЬ⟫ в report.md"
