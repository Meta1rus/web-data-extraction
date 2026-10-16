#!/usr/bin/env bash
# Запуск n8n для no-code частей лаб 3–7 и итогового проекта (macOS).
# Установка один раз:  source ~/.nvm/nvm.sh && nvm install 24 && nvm alias default 24 && npm install -g n8n
# Интерфейс: http://localhost:5678 (при первом входе — создать локального владельца).
set -euo pipefail

if [ -s "$HOME/.nvm/nvm.sh" ]; then
  # shellcheck disable=SC1091
  source "$HOME/.nvm/nvm.sh"
  nvm use 24 >/dev/null 2>&1 || echo "nvm: Node 24 не найден — установите: nvm install 24"
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$NODE_MAJOR" -lt 24 ]; then
  echo "n8n 2.x требует Node.js >= 24, сейчас: $(node --version 2>/dev/null || echo 'нет node')."
  echo "Установите: source ~/.nvm/nvm.sh && nvm install 24 && nvm alias default 24"
  exit 1
fi

# Каталоги, куда workflow пишут CSV, журналы ошибок и оповещения
mkdir -p "$HOME/.n8n-files/lab6" "$HOME/.n8n-files/podolskiy"

# Узлы чтения/записи файлов работают только внутри этого каталога (n8n >= 1.9x)
export N8N_RESTRICT_FILE_ACCESS_TO="$HOME/.n8n-files"
# Пути к файлам в workflow строятся из $env.HOME (n8n 2.x по умолчанию запрещает $env в выражениях)
export N8N_BLOCK_ENV_ACCESS_IN_NODE=false
# Узел Execute Command (итоговый проект запускает пайплайн) в n8n 2.x по умолчанию выключен
export NODES_EXCLUDE="[]"

echo "n8n $(n8n --version 2>/dev/null || echo '?'), Node $(node --version). Откройте http://localhost:5678"
if command -v n8n >/dev/null 2>&1; then
  exec n8n start
else
  exec npx --yes n8n start
fi
