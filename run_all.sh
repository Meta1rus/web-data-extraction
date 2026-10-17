#!/usr/bin/env bash
# Реальный прогон всех лабораторных, которым нужен доступ к учебным сайтам (macOS, bash 3.2+).
#
#   ./run_all.sh                 лабы 3, 4, 5, 7 (≈ 40–50 мин)
#   ./run_all.sh --with-github   плюс повторный прогон лабы 6 и итогового проекта (данные уже собраны 15.10.2026)
#   ./run_all.sh 3 4             только перечисленные лабы
#
# Переменные окружения (необязательно):
#   PYTHON=python3.13            интерпретатор для создания .venv в каждой лабе (нужен Python >= 3.10)
#   START_URL=https://scrapeme.live/shop/   источник для лаб 5 и 7 — ТОЛЬКО после согласования с преподавателем
#                                (scrapingcourse.com/ecommerce закрыт в robots.txt, см. solutions/lab5_podolskiy_5/README.md)
#
# Каждая лаба пишет свой журнал в <лаба>/logs/real_run_*.log; общий журнал — logs/run_all_<время>.log.
# Сбой одной лабы не останавливает остальные; итоговая таблица — в конце.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs
TS="$(date +%Y%m%d_%H%M%S)"
LOG="logs/run_all_${TS}.log"
exec > >(tee -a "$LOG") 2>&1

PY="${PYTHON:-python3}"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "Нужен Python >= 3.10. Сейчас: $("$PY" --version 2>&1). Пример: PYTHON=python3.13 ./run_all.sh"
  exit 1
fi
export PYTHON="$PY"
echo "== Общий прогон: $(date '+%Y-%m-%d %H:%M:%S'), $("$PY" --version), журнал $LOG"

WITH_GITHUB=0
SELECTED=""
for a in "$@"; do
  case "$a" in
    --with-github) WITH_GITHUB=1 ;;
    3|4|5|6|7|final) SELECTED="$SELECTED $a" ;;
    *) echo "Неизвестный аргумент: $a"; exit 2 ;;
  esac
done
if [ -z "$SELECTED" ]; then
  SELECTED="3 4 5 7"
  [ "$WITH_GITHUB" -eq 1 ] && SELECTED="$SELECTED 6 final"
fi

dir_for() {
  case "$1" in
    final) echo "solutions/final_project_podolskiy" ;;
    *) echo "solutions/lab$1_podolskiy_5" ;;
  esac
}

RESULTS=""
for lab in $SELECTED; do
  d="$(dir_for "$lab")"
  echo
  echo "================================================================"
  echo "== Лаба $lab: $d — старт $(date '+%H:%M:%S')"
  echo "================================================================"
  if [ ! -f "$d/run_real.sh" ]; then
    echo "нет $d/run_real.sh — пропускаю"
    RESULTS="$RESULTS
$lab|нет скрипта|0"
    continue
  fi
  if [ "$lab" = "6" ] || [ "$lab" = "final" ]; then
    [ -f "$d/.env" ] || cp "$d/.env.example" "$d/.env"
    if ! grep -qE '^GITHUB_TOKEN=.+' "$d/.env" && [ -z "${GITHUB_TOKEN:-}" ]; then
      echo "ВНИМАНИЕ: в $d/.env нет GITHUB_TOKEN — без токена лимит API 60 запросов/час"
    fi
  fi
  t0=$(date +%s)
  ( cd "$d" && bash ./run_real.sh )
  rc=$?
  RESULTS="$RESULTS
$lab|код $rc|$(( $(date +%s) - t0 ))"
done

echo
echo "================ Итог ================"
printf "%-7s %-12s %s\n" "лаба" "результат" "время, с"
echo "$RESULTS" | while IFS='|' read -r lab res sec; do
  [ -n "$lab" ] && printf "%-7s %-12s %s\n" "$lab" "$res" "$sec"
done
echo
echo "Дальше:"
echo "  1) n8n: ./start_n8n.sh, затем по nocode/INSTRUCTIONS.md каждой лабы — импорт workflow.json, запуск, скриншоты;"
echo "  2) цифры для отчётов — в журналах logs/real_run_*.log и отчётах reports/ каждой лабы."
echo "Журнал: $LOG"
