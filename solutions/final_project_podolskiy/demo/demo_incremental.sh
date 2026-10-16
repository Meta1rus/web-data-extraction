#!/usr/bin/env bash
# Сценарий 1 защиты (30–60 c): быстрый инкрементальный прогон.
# Одна тема, одна страница; все 20 репозиториев перепроверяются условными запросами (API — If-Modified-Since, HTML — If-None-Match) ->
# неизменившиеся отвечают 304 (без тела; с токеном квоту не расходуют), UPSERT не создаёт дублей, печатается отчёт.
# Предусловие: уже был полный прогон (заполнены db/warehouse.db и state/etags.json).
# Для локальной имитации: GITHUB_HTML_BASE=http://127.0.0.1:28765 GITHUB_API_BASE=http://127.0.0.1:28766 bash demo/demo_incremental.sh --rate-scale 20
set -euo pipefail
source "$(dirname "$0")/_common.sh"
set +e
"$PY" src/run_pipeline.py --topics web-scraping --max-pages 1 --refresh-max-age-hours 0 "$@"
code=$?
set -e
echo; show_latest_report
echo "Код возврата пайплайна: $code (0 — зелёный/жёлтый)"
exit "$code"
