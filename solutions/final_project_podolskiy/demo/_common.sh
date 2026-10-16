# Общие настройки демонстрационных сценариев (подключается через source).
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
show_latest_report() {
  "$PY" - <<'PYEOF'
import json, os
from pathlib import Path
root = Path(os.getenv("PIPELINE_DATA_ROOT") or ".")
r = json.loads((root / "reports" / "latest_run_report.json").read_text(encoding="utf-8"))
q = r["requests"]
quota = (q.get("api_quota") or {}).get("used_delta_by_status", {})
print(f"Итог: вердикт {r['verdict']}, код {r['exit_code']}, длительность {r['duration_s']} c")
print(f"Запросов {q['total']}, коды {q['by_status']}; 304: {q['not_modified_304']} из {q['conditional_sent']} условных")
print(f"Расход квоты API по кодам ответа (X-RateLimit-Used): {quota}")
st = r.get("storage", {}).get("repos", {})
print(f"UPSERT repos: вставлено {st.get('inserted')}, обновлено {st.get('updated')}, без изменений {st.get('unchanged')}")
for inc in r.get("incidents", []):
    print(f"Инцидент: {inc['kind']} — {inc['message']} ({inc.get('path')})")
PYEOF
}
