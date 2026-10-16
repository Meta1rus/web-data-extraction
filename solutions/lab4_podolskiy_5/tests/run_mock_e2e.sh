#!/usr/bin/env bash
# Сквозная проверка всего пайплайна на локальной имитации сайта (без интернета).
# Результаты пишутся во временную папку (OUT_DIR), рабочая папка лабораторной не меняется.
#   bash tests/run_mock_e2e.sh [OUT_DIR]
set -euo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$(mktemp -d -t lab4_mock_XXXX)}"
PORT="${MOCK_PORT:-8765}"
PY="${PYTHON:-python3}"
mkdir -p "$OUT"

"$PY" "$LAB/tests/mock_site.py" --port "$PORT" > "$OUT/mock_server.log" 2>&1 &
MOCK_PID=$!
trap 'kill "$MOCK_PID" 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do                      # ждём готовности сервера (до 5 с)
  curl -fs -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 0.1
done

export BASE_URL="http://127.0.0.1:$PORT" OUT_DIR="$OUT" POLITE_PAUSE="${POLITE_PAUSE:-0.05}"
run() { echo; echo "=== $* ==="; "$@"; }
cd "$LAB"
run "$PY" diagnose.py
run "$PY" collect.py --trace            # 1-й запуск: вход формой, storage_state сохраняется
run "$PY" collect.py --auth-check > "$OUT/auth_check.log"   # 2-й запуск: сессия из файла
cat "$OUT/auth_check.log"
grep -q "сессия восстановлена" "$OUT/auth_check.log" && echo "OK: повторный вход не выполнялся (storage_state)"
run "$PY" light.py --force-login        # вход через CSRF без браузера
run "$PY" compare.py
run "$PY" bench.py --reps 2
run "$PY" faults.py
run "$PY" parallel.py --max-tags 20
run "$PY" summarize.py

echo; echo "=== блокнот (nbclient) ==="
"$PY" - "$LAB" "$OUT" <<'EOF'
import sys, nbformat
from nbclient import NotebookClient
lab, out = sys.argv[1], sys.argv[2]
nb = nbformat.read(f"{lab}/lab4.ipynb", as_version=4)
NotebookClient(nb, timeout=300, kernel_name="python3", resources={"metadata": {"path": lab}}).execute()
nbformat.write(nb, f"{out}/lab4_executed.ipynb")
errs = [o for c in nb.cells if c.cell_type == "code" for o in c.get("outputs", []) if o.get("output_type") == "error"]
print("ячеек:", len(nb.cells), "| ошибок:", len(errs))
sys.exit(1 if errs else 0)
EOF
echo; echo "Готово. Результаты имитации: $OUT"
