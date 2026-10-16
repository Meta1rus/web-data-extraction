#!/usr/bin/env bash
# Сценарий 2 защиты: демонстрация устойчивости.
# API-источник «недоступен» (неверный адрес): временные ошибки -> повторы с backoff -> предохранитель размыкается ->
# остальные запросы к домену не отправляются -> инцидент в logs/incidents/ -> прогон деградирует предсказуемо
# (HTML-часть и mart работают на ранее собранных метриках) -> код возврата 3.
#   bash demo/demo_failure.sh            # отказ API (по умолчанию)
#   FAULT=html bash demo/demo_failure.sh # отказ HTML-источника: ключи для API берутся из БД
set -euo pipefail
source "$(dirname "$0")/_common.sh"
FAULT="${FAULT:-api}"
if [ "$FAULT" = "html" ]; then
  export GITHUB_HTML_BASE="https://github.invalid"
else
  export GITHUB_API_BASE="https://api.github.invalid"
fi
echo "Имитация отказа источника: $FAULT (неверный адрес .invalid — гарантированно не существует, RFC 2606)"
set +e
"$PY" src/run_pipeline.py --topics web-scraping --max-pages 1 --refresh-max-age-hours 0 "$@"
code=$?
set -e
echo; show_latest_report
echo "Код возврата пайплайна: $code (ожидается 3 — источник недоступен, отказ изолирован)"
latest_incident=$(ls -1t "${PIPELINE_DATA_ROOT:-.}"/logs/incidents/*.json 2>/dev/null | head -1 || true)
[ -n "$latest_incident" ] && { echo "Последний инцидент: $latest_incident"; cat "$latest_incident"; echo; }
exit "$code"
