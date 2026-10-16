#!/usr/bin/env bash
# Запасной сценарий: возобновляемость. Прогон «обрывается» после N сетевых запросов (код 130),
# состояние (state/checkpoint.json, водяной знак, ETag, сырой слой) сохранено; повторный запуск продолжает
# с места остановки: собранные страницы берутся из raw/, обработанные ключи API не запрашиваются повторно.
set -euo pipefail
source "$(dirname "$0")/_common.sh"
N="${N:-12}"
ARGS=(--topics web-scraping,cli --max-pages 2 --refresh-max-age-hours 0 "$@")
set +e
"$PY" src/run_pipeline.py "${ARGS[@]}" --simulate-crash-after "$N"
echo "Код возврата первого запуска: $? (ожидается 130 — прерван)"
"$PY" -c "import json,os;c=json.load(open(os.path.join(os.getenv('PIPELINE_DATA_ROOT') or '.','state','checkpoint.json')));print('checkpoint:', c['status'], 'страниц HTML:', sum(len(v) for v in c['html_done'].values()), 'ключей API:', len(c['api_done']))"
"$PY" src/run_pipeline.py "${ARGS[@]}"
code=$?
set -e
echo "Код возврата второго запуска: $code"
exit "$code"
