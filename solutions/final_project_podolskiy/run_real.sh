#!/usr/bin/env bash
# Реальный прогон итогового проекта на macOS — всё по порядку, с журналом logs/real_run_<время>.log.
# Перед запуском: cp .env.example .env и вписать GITHUB_TOKEN (без токена — режим search, 60 запр/ч).
# Ожидаемая длительность: ≈ 20–25 мин + установка пакетов (оценка по лимитам config.yaml).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/real_run_$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1
echo "== Итоговый проект (Подольский): реальный прогон, журнал $LOG"
date -u

# Реальные адреса источников: переопределения для имитации сбрасываем
unset GITHUB_HTML_BASE GITHUB_API_BASE PIPELINE_DATA_ROOT || true

PY_SYS="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "== Создаю виртуальное окружение .venv ($("$PY_SYS" --version))"
  "$PY_SYS" -m venv .venv
fi
PY=.venv/bin/python
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt
export PY

[ -f .env ] || cp .env.example .env
if [ -z "${GITHUB_TOKEN:-}" ] && ! grep -qE '^GITHUB_TOKEN=.+' .env; then
  echo "ВНИМАНИЕ: GITHUB_TOKEN не задан — API в режиме search (без токена: 60 запр/ч, Search 10 запр/мин)."
  echo "          Демонстрация «304 не расходует квоту» требует токена."
fi

CODES=()
step() {
  local name="$1"; shift
  echo; echo "== $name"; echo "   $(date -u +%H:%M:%S) UTC: $*"
  set +e; "$@"; local c=$?; set -e
  echo "-- код возврата: $c"
  CODES+=("$name -> $c")
}

step "1. Тесты (pytest на фикстурах)" "$PY" -m pytest -q tests
step "2. Диагностика HTML-источника и robots.txt" "$PY" src/collect_html.py --diagnose
step "3. Прогон 1: полный сбор" "$PY" src/run_pipeline.py

snap=$(ls -1t raw/html_pages/web-scraping/p01_*.html.gz 2>/dev/null | head -1 || true)
if [ -n "$snap" ]; then
  gunzip -c "$snap" > tests/fixtures/real_topic_p1.html
  echo "Реальная страница темы сохранена как фикстура: tests/fixtures/real_topic_p1.html"
fi
step "4. Тест парсера на реальной странице" "$PY" -m pytest -q tests/test_parser.py -k "real_page or cards"
step "5. Прогон 2: сразу же (водяной знак)" "$PY" src/run_pipeline.py
step "6. Прогон 3: перепроверка всех репозиториев с ETag" "$PY" src/run_pipeline.py --refresh-max-age-hours 0
step "7. Демо: быстрый инкрементальный прогон (304)" bash demo/demo_incremental.sh
step "8. Демо: отказ API-источника" bash demo/demo_failure.sh
step "9. Демо: обрыв и возобновление" bash demo/demo_resume.sh
step "10. Быстрый штатный прогон после демо (свежий отчёт для блокнота)" "$PY" src/run_pipeline.py --topics web-scraping --max-pages 1

NB_RUN='
import nbformat
from nbclient import NotebookClient
nb = nbformat.read("analysis.ipynb", as_version=4)
NotebookClient(nb, timeout=900, kernel_name="python3", resources={"metadata": {"path": "."}}).execute()
nbformat.write(nb, "analysis.ipynb")
errors = [o for c in nb.cells if c.cell_type == "code" for o in c.outputs if o.get("output_type") == "error"]
print("analysis.ipynb исполнен, ошибок:", len(errors))
'
# ядро python3 — из .venv (JUPYTER_PATH имеет приоритет над пользовательскими ядрами)
step "11. analysis.ipynb на реальных данных (выводы, графики mart/fig*.png)" \
  env JUPYTER_PATH="$PWD/.venv/share/jupyter" "$PY" -c "$NB_RUN"

echo; echo "== Сводка шагов"
for c in "${CODES[@]}"; do echo "   $c"; done
echo; echo "== Прогоны (pipeline_runs)"
"$PY" - <<'EOF'
import sqlite3
c = sqlite3.connect("db/warehouse.db")
print(f"{'run_id':18} {'статус':11} {'вердикт':7} {'код':>3} {'карточек':>8} {'200':>5} {'304':>5} {'+ins':>5} {'upd':>5} {'same':>5}")
for r in c.execute("SELECT run_id, status, verdict, exit_code, html_cards, api_200, api_304, inserted, updated, unchanged "
                   "FROM pipeline_runs ORDER BY started_at"):
    print(" ".join(f"{'' if v is None else v!s:>5}" if i > 2 else f"{v!s:{[18, 11, 7][i]}}" for i, v in enumerate(r)))
EOF
echo
echo "Готово. Дальше вручную:"
echo "  1) перенести цифры в места ⟪ЗАПОЛНИТЬ⟫ (README §8 перечисляет их) из reports/ и analysis.ipynb;"
echo "  2) n8n: nocode/INSTRUCTIONS.md (импорт workflow.json, запуск, скриншоты nocode/screenshots/screenshot_*.png);"
echo "  3) скриншоты диагностики (View Source / Network) — docs/decisions.md §3; slides.md -> slides.pdf."
date -u
