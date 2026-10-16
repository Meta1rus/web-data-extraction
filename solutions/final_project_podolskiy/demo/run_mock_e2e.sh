#!/usr/bin/env bash
# Полная офлайн-проверка на ЛОКАЛЬНОЙ ИМИТАЦИИ (синтетические данные) в отдельном каталоге demo/_mock_run/:
# три прогона UPSERT (вставка -> нули -> обновления), быстрый инкрементальный прогон, обрыв и возобновление,
# отказ API, pytest и исполнение analysis.ipynb. Реальные данные проекта не затрагиваются.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"; [ -x "$PY" ] || PY="python3"
export PIPELINE_DATA_ROOT="$PWD/demo/_mock_run"
rm -rf "$PIPELINE_DATA_ROOT"; mkdir -p "$PIPELINE_DATA_ROOT"
export GITHUB_HTML_BASE=http://127.0.0.1:28765 GITHUB_API_BASE=http://127.0.0.1:28766
export GITHUB_TOKEN="mock-local-token-not-a-secret"
"$PY" demo/mock_github.py > "$PIPELINE_DATA_ROOT/mock_server.log" 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do curl -sf http://127.0.0.1:28766/__control >/dev/null 2>&1 && break; sleep 0.2; done
ctl() { curl -sf "http://127.0.0.1:28766/__control?$1" >/dev/null; }
run() { set +e; "$PY" src/run_pipeline.py --rate-scale 100 --quiet "$@"; local c=$?; set -e; echo "   -> код $c"; }
echo "== Прогон 1: первичная загрузка";             run
echo "== Прогон 2: повтор сразу (водяной знак)";    run
ctl "version=v2"
echo "== Прогон 3: данные v2, перепроверка с ETag"; run --refresh-max-age-hours 0
echo "== Быстрый инкрементальный прогон (304)";      run --topics web-scraping --max-pages 1 --refresh-max-age-hours 0
echo "== Обрыв и возобновление";                      run --topics cli --max-pages 2 --refresh-max-age-hours 0 --simulate-crash-after 8; run --topics cli --max-pages 2 --refresh-max-age-hours 0
echo "== Отказ API (неверный адрес)";                 GITHUB_API_BASE=https://api.github.invalid run --topics web-scraping --max-pages 1 --refresh-max-age-hours 0
echo "== pytest";                                     "$PY" -m pytest -q tests
echo "== analysis.ipynb (копия с выводами: $PIPELINE_DATA_ROOT/analysis_mock.ipynb)"
JUPYTER_PATH="$PWD/.venv/share/jupyter" "$PY" - <<'PYEOF'
import os, nbformat
from nbclient import NotebookClient
nb = nbformat.read("analysis.ipynb", as_version=4)
NotebookClient(nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": "."}}).execute()
out = os.path.join(os.environ["PIPELINE_DATA_ROOT"], "analysis_mock.ipynb")
nbformat.write(nb, out)
errors = [o for c in nb.cells if c.cell_type == "code" for o in c.outputs if o.get("output_type") == "error"]
print("ошибок в блокноте:", len(errors))
PYEOF
"$PY" - <<'PYEOF'
import sqlite3, os
c = sqlite3.connect(os.path.join(os.environ["PIPELINE_DATA_ROOT"], "db", "warehouse.db"))
print("Прогоны (run_id, статус, вердикт, код, вставлено, обновлено, без изменений):")
for r in c.execute("SELECT run_id, status, verdict, exit_code, inserted, updated, unchanged FROM pipeline_runs ORDER BY started_at"):
    print("  ", r)
PYEOF
