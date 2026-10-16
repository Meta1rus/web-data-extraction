#!/usr/bin/env bash
# Подготовка итогового проекта на Mac для n8n: виртуальное окружение .venv и файл .env.
# Сбор данных не запускает (реальные данные уже собраны 15.10.2026).
#   bash setup_mac.sh            (из папки final_project_podolskiy)
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Нужен Python >= 3.10, сейчас: $("$PY" --version 2>&1)"; exit 1; }
if [ ! -x .venv/bin/python ]; then
  echo "Создаю .venv ($("$PY" --version))"
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Готово: .venv создан, зависимости установлены."
if grep -qE '^GITHUB_TOKEN=.+' .env; then
  echo "GITHUB_TOKEN в .env задан."
else
  echo "GITHUB_TOKEN в .env пуст — пайплайн будет работать в режиме Search API (без токена этого хватает для запуска из n8n)."
fi
echo "Проверка: .venv/bin/python src/run_pipeline.py --topics web-scraping --max-pages 1"
