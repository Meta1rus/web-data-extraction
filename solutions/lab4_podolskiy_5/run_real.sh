#!/usr/bin/env bash
# ЛР-4, вариант 5 — реальный прогон на https://quotes.toscrape.com (macOS / Linux, Python >= 3.10).
# Делает всё по порядку: venv → зависимости → Chromium → диагностика → браузерный сбор →
# дешёвая альтернатива → сверка → замеры → бонусы → сводка → исполнение блокнота.
# Лог: logs/real_run_<время>.log. Ориентировочно 15–25 мин (оценка, не замер).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/real_run_$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "Старт: $(date '+%F %T') | лог: $LOG"

PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python --version
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
python -m playwright install chromium          # загрузка ~120 МБ при первом запуске (по данным занятия 4)
[ -f .env ] || cp .env.example .env

# только реальный сайт и папка лабораторной (переопределяет тестовые значения из .env)
export BASE_URL="https://quotes.toscrape.com"
export OUT_DIR="$PWD"
export POLITE_PAUSE="${POLITE_PAUSE:-1.0}"
export PW_CHROMIUM_PATH=""          # пустое значение не даст .env подставить тестовый путь

FAILED=()
step() {
  local name="$1"; shift
  echo; echo "=== $(date +%H:%M:%S) $name: $* ==="
  local t0=$SECONDS rc=0
  "$@" || rc=$?
  echo "--- $name: код $rc, $((SECONDS - t0)) с"
  if [ "$rc" -ne 0 ]; then FAILED+=("$name(код $rc)"); fi
  return 0
}

step diagnose  python diagnose.py
step browser   python collect.py --trace         # 1-й запуск: вход формой, сохраняется auth_state.json
step authcheck python collect.py --auth-check    # новый контекст из auth_state.json — без повторного входа
step light     python light.py                   # requests.Session: CSRF-вход без браузера
step compare   python compare.py
step bench     python bench.py --reps 3
step faults    python faults.py                  # бонус: подмена ответов route.fulfill + trace
step parallel  python parallel.py --workers 3 --max-tags 30   # бонус: параллельные контексты
step summarize python summarize.py

echo; echo "=== $(date +%H:%M:%S) notebook: исполнение lab4.ipynb ==="
python - <<'EOF' || FAILED+=("notebook")
import sys, nbformat
from nbclient import NotebookClient
nb = nbformat.read("lab4.ipynb", as_version=4)
NotebookClient(nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": "."}}).execute()
nbformat.write(nb, "lab4.ipynb")
errs = [o for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", []) if o.get("output_type") == "error"]
print("lab4.ipynb исполнен, ошибок в ячейках:", len(errs))
sys.exit(1 if errs else 0)
EOF

echo
echo "Готово: $(date '+%F %T')"
echo "Данные: data_browser.csv, data_light.csv, collection_log.csv | метрики: metrics/ | сводка для отчёта: metrics/summary.md"
grep -q "сессия восстановлена" "$LOG" && echo "storage_state: повторный запуск прошёл без входа — OK"
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "Шаги с ненулевым кодом: ${FAILED[*]} — см. лог $LOG и artifacts/"
  exit 1
fi
echo "Все шаги завершились с кодом 0. Дальше: nocode/INSTRUCTIONS.md (n8n) и маркеры ⟪ЗАПОЛНИТЬ⟫ в report.md."
