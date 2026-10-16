#!/usr/bin/env bash
# Лабораторная № 6, вариант 5 — реальный прогон на macOS:
# сбор (GitHub API + HTML) → синтетическая порча 7 % → конвейер (clean, БД, метрики, mart)
# → подстановка цифр в report.md → исполнение quality_report.ipynb.
# Лог: logs/real_run_<UTC>.log. Код выхода 2 — «светофор» красный, mart не обновлён.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
LOG="logs/real_run_$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "$LOG") 2>&1
echo "== Лаб. 6 (вариант 5): реальный прогон, старт $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
START=$(date +%s)

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Нужен Python ≥ 3.10, а $PY — $("$PY" --version 2>&1). Запуск: PYTHON=python3.13 ./run_real.sh"; exit 1; }
if [ ! -d .venv ]; then
  echo "== Создаю виртуальное окружение .venv ($("$PY" --version))"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

if [ -f .env ]; then set -a; source .env; set +a; fi
# Только реальные адреса и папка лабораторной (эти переменные нужны лишь тестам на имитации)
unset GITHUB_API_URL GITHUB_WEB_URL LAB6_DATA_ROOT
python -c "import os; t=os.getenv('GITHUB_TOKEN',''); print('GITHUB_TOKEN:', t[:4]+'…'+t[-2:] if len(t) > 8 else '<нет — лимит 60 запросов/час>')"

echo "== 1/5 Сбор: GitHub API + HTML-страницы (паузы 1 с / 3 с, robots.txt)"
set +e
python -m src.collect --repos config/repos.txt --max-issue-pages "${MAX_ISSUE_PAGES:-3}" --since auto
rc=$?
set -e
if [ "$rc" -ne 0 ] && [ "$rc" -ne 3 ]; then echo "Сбор завершился с ошибкой ($rc)"; exit "$rc"; fi
[ "$rc" -eq 3 ] && echo "⚠ Лимит API исчерпан — продолжаю с тем, что собрано (подробности выше)"

echo "== 2/5 Синтетическая порча 7 % новой партии (отдельный файл raw/synthetic_dirty.jsonl)"
python -m src.corrupt --share 0.07 --seed 5

echo "== 3/5 Конвейер: clean → пересборка → форматы → БД → метрики → mart → DuckDB"
set +e
python -m src.pipeline
prc=$?
set -e

echo "== 4/5 Подстановка цифр в report.md"
python -m src.fill_report

echo "== 5/5 Исполнение quality_report.ipynb (на месте)"
jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=900 quality_report.ipynb

echo "== Готово за $(( $(date +%s) - START )) с. Лог: $LOG. Код конвейера: $prc (2 = светофор красный)"
exit "$prc"
