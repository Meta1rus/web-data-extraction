#!/usr/bin/env bash
# Реальный прогон лабораторной № 7 (вариант 5 «Scrapy-краулер») на macOS / Linux.
#
#   ./run_real.sh                                         # источник варианта (scrapingcourse.com/ecommerce)
#   START_URL=https://scrapeme.live/shop/ ./run_real.sh   # ТОЛЬКО после согласования замены источника
#   HTTPBIN=https://httpbin.org ./run_real.sh             # стенд кодов ответа (по умолчанию httpbin.org)
#
# Порядок: venv → robots.txt → прогоны A/B/C и экономия (если обход разрешён) → прерывание и
# возобновление → предохранитель (второй домен httpbin) → коды ответа на httpbin → сценарии на
# локальной имитации (429 с Retry-After, AutoThrottle, заглушка, история) → лимит 4 процессов →
# таблицы → блокнот analysis.ipynb. Весь вывод — в logs/real_run_<время>.log.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs/incidents reports state data
LOG="logs/real_run_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

START_URL="${START_URL:-https://www.scrapingcourse.com/ecommerce/}"
HTTPBIN="${HTTPBIN:-https://httpbin.org}"
PY="${PYTHON:-python3}"
echo "== Лабораторная 7: старт $(date '+%F %T'); источник: $START_URL; стенд кодов: $HTTPBIN"

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
python -c "import scrapy, sys; print('Python', sys.version.split()[0], '| Scrapy', scrapy.__version__)"

# Коды возврата сборщика — часть результата (1 красный статус, 2 блокировка, 3 robots.txt),
# поэтому каждый запуск оборачиваем: скрипт не прерывается, код пишется в журнал.
collect() {
  set +e
  python -m src.collect "$@"
  local rc=$?
  set -e
  echo "== src.collect $* → код возврата $rc"
  return 0
}

# 1. robots.txt источника и стенда
set +e
python -m src.collect --robots-only --start-url "$START_URL"
ROBOTS_RC=$?
set -e
collect --robots-only --start-url "$HTTPBIN/"

if [ "$ROBOTS_RC" -eq 0 ]; then
  # 2. A/B/C на одинаковом объёме: 1 страница каталога + её карточки (≈ 18 запросов)
  collect --mode A --pages 1 --max-requests 30 --fresh-state --label A --start-url "$START_URL"
  sleep 10
  collect --mode B --pages 1 --fresh-state --label B --start-url "$START_URL"
  collect --mode C --pages 1 --label C --start-url "$START_URL"
  # 3. Экономия по механизмам
  collect --mode B --pages 1 --fresh-state --no-history --label S0_base --start-url "$START_URL"
  collect --mode B --pages 1 --cond --no-history --label S1_cond --start-url "$START_URL"
  collect --mode B --pages 1 --gzip --no-history --label S2_gzip --start-url "$START_URL"
  collect --mode B --pages 1 --cond --incremental --no-history --label S3_incr --start-url "$START_URL"
  # 4. Прерывание (SIGINT через 10 с) и возобновление из JOBDIR, 2 страницы
  python tools/resume_demo.py --start-url "$START_URL" --pages 2 --after 10 --prefix real_resume
  # 5. Предохранитель: второй домен (httpbin) «падает» на первые 6 с (/status/503), затем «здоров»
  #    (/status/200); каталог (2 стр.) собирается дальше без ошибок
  collect --mode B --pages 2 --fresh-state --no-history --label real_breaker --start-url "$START_URL" \
      --extra-fail-url "$HTTPBIN/status/503" --extra-url "$HTTPBIN/status/200" --extra-outage-s 6 \
      --breaker-reset 5
else
  echo "== robots.txt запрещает обход $START_URL (код $ROBOTS_RC): прогоны краулера на реальном сайте"
  echo "== пропущены. Нужна замена источника по согласованию (README, раздел про robots.txt)."
fi

# 6. Коды ответа на httpbin: 429 без Retry-After, 401, 403, 404, ETag, gzip, разбор Retry-After
RA_DATE=$(python -c "from email.utils import formatdate; import time, urllib.parse; print(urllib.parse.quote(formatdate(time.time()+30, usegmt=True)))")
collect --label httpbin_codes --cond --gzip --fresh-state --no-history --probe-urls \
"$HTTPBIN/status/429|$HTTPBIN/status/401|$HTTPBIN/status/403|$HTTPBIN/status/404|$HTTPBIN/etag/lab7-v1|$HTTPBIN/gzip|$HTTPBIN/response-headers?Retry-After=3|$HTTPBIN/response-headers?Retry-After=$RA_DATE"
collect --label httpbin_etag_repeat --cond --no-history --probe-urls "$HTTPBIN/etag/lab7-v1"
collect --label httpbin_503_timeout --timeout 5 --breaker-reset 10 --no-history --probe-urls \
"$HTTPBIN/status/503|$HTTPBIN/delay/10|$HTTPBIN/status/503?x=2|$HTTPBIN/status/200"

# 7. Сценарии, которые воспроизводим только на собственном стенде (порты 18507–18509)
python tools/run_mock_scenarios.py
python tools/demo_shared_limit.py

# 8. Таблицы и блокнот
python tools/make_comparison.py > /dev/null
python -m jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=600 analysis.ipynb

echo "== Готово $(date '+%F %T'). Журнал: $LOG"
echo "== Сравнение — reports/comparison.md, анализ — analysis.ipynb; n8n — nocode/INSTRUCTIONS.md"
