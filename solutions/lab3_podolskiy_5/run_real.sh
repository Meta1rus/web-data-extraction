#!/usr/bin/env bash
# ЛР № 3 (вариант 5, CoinGecko): реальный прогон Python-части на macOS / Linux.
#
#   ./run_real.sh            исполнить lab3.ipynb, дописав данные к уже собранным
#   ./run_real.sh --fresh    сначала перенести прошлые результаты в logs/backup_<время>/
#
# Что делает: создаёт .venv (если его нет), ставит зависимости, регистрирует ядро Jupyter
# внутри .venv, исполняет lab3.ipynb сверху вниз с сохранением выводов в сам блокнот,
# печатает сводку. Весь вывод дублируется в logs/real_run_<время>.log.
# Часть n8n выполняется вручную — см. nocode/INSTRUCTIONS.md.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
TS="$(date +%Y%m%d_%H%M%S)"
LOG="logs/real_run_${TS}.log"
exec > >(tee -a "$LOG") 2>&1

echo "== ЛР3, вариант 5: реальный прогон, $(date -u +%Y-%m-%dT%H:%M:%SZ) UTC =="

# Адрес имитации допустим только в тестах: для реального прогона снимаем его
if [[ -n "${COINGECKO_BASE_URL:-}" ]]; then
  echo "Переменная COINGECKO_BASE_URL была задана (${COINGECKO_BASE_URL}) — снята, запросы пойдут в api.coingecko.com"
  unset COINGECKO_BASE_URL
fi
if [[ -f .env ]] && grep -q '^COINGECKO_BASE_URL=' .env; then
  echo "ОШИБКА: в .env задан COINGECKO_BASE_URL — удалите эту строку, иначе сбор пойдёт не в CoinGecko" >&2
  exit 1
fi

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "нужен Python >= 3.10, сейчас " + sys.version'
if [[ ! -d .venv ]]; then
  echo "Создаю виртуальное окружение .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
python -m ipykernel install --sys-prefix --name lab3-podolskiy --display-name "Python (lab3)" > /dev/null
echo "Python: $(python --version), окружение: .venv"

if [[ "${1:-}" == "--fresh" ]]; then
  BACKUP="logs/backup_${TS}"
  mkdir -p "$BACKUP"
  for f in data_raw.jsonl data_clean.csv collection_log.csv state.json; do
    if [[ -f "$f" ]]; then mv "$f" "$BACKUP/"; fi
  done
  echo "Прошлые результаты перенесены в $BACKUP"
fi

if [[ -n "${COINGECKO_DEMO_API_KEY:-}" ]] || { [[ -f .env ]] && grep -Eq '^COINGECKO_DEMO_API_KEY=.+' .env; }; then
  echo "Demo-ключ найден (значение не печатается); ожидаемое время исполнения блокнота — около 4 минут"
else
  echo "Demo-ключ не задан — keyless-режим; ожидаемое время исполнения блокнота — около 10 минут"
fi

START="$(date +%s)"
jupyter nbconvert --to notebook --execute --inplace lab3.ipynb \
  --ExecutePreprocessor.timeout=3600 \
  --ExecutePreprocessor.kernel_name=lab3-podolskiy
END="$(date +%s)"
echo "Блокнот исполнен за $((END - START)) с; выводы сохранены в lab3.ipynb"

python - <<'PYEOF'
import json
import pandas as pd

clean = pd.read_csv("data_clean.csv", keep_default_na=False, na_values=[""])
state = json.load(open("state.json", encoding="utf-8"))
with open("data_raw.jsonl", encoding="utf-8") as f:
    raw_lines = sum(1 for _ in f)
print(f"data_clean.csv:     {len(clean)} строк, снимки: {sorted(clean['snapshot_date'].astype(str).unique())}")
print(f"data_raw.jsonl:     {raw_lines} строк")
print(f"collection_log.csv: {len(pd.read_csv('collection_log.csv'))} строк")
keys = ("run_id", "duration_s", "http_attempts", "pages", "unique_records", "new_records", "enriched",
        "retries", "status_429", "status_5xx", "status_304")
for run in state["runs"][-2:]:
    print({k: run.get(k) for k in keys})
PYEOF

echo "Лог прогона: $LOG"
echo "Дальше: часть n8n — nocode/INSTRUCTIONS.md; числа для report.md печатает последняя ячейка блокнота (этап 12)."
