#!/usr/bin/env bash
# Запуск n8n для no-code частей лаб 3–7 и итогового проекта (macOS).
# Установка один раз:  source ~/.nvm/nvm.sh && nvm install 24 && nvm alias default 24 && npm install -g n8n
# Интерфейс: http://localhost:5678 (при первом входе — создать локального владельца).
# Скрипт не зависит от того, какой Node включён в текущем окне Терминала: он сам находит
# Node >= 24, установленный через nvm (~/.nvm/versions/node/v24.x), и ставит его первым в PATH.
set -eo pipefail

pick_node_bin() {
  local best="" d
  for d in "$HOME"/.nvm/versions/node/v*/bin; do
    [ -x "$d/node" ] || continue
    local ver="${d%/bin}"; ver="${ver##*/v}"
    [ "${ver%%.*}" -ge 24 ] 2>/dev/null && best="$d"   # глоб отсортирован — берём последний подходящий
  done
  printf '%s' "$best"
}

NODE_BIN="$(pick_node_bin)"
if [ -n "$NODE_BIN" ]; then
  export PATH="$NODE_BIN:$PATH"
  hash -r
fi

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$NODE_MAJOR" -lt 24 ]; then
  echo "n8n 2.x требует Node.js >= 24, найден: $(node --version 2>/dev/null || echo 'нет node') ($(command -v node || echo '-'))."
  echo "Установите Node 24 через nvm: source ~/.nvm/nvm.sh && nvm install 24 && nvm alias default 24"
  exit 1
fi

# Каталоги, куда workflow пишут CSV, журналы ошибок и оповещения (по папке на работу)
mkdir -p "$HOME/.n8n-files/lab3" "$HOME/.n8n-files/lab4" "$HOME/.n8n-files/lab5" "$HOME/.n8n-files/lab6" "$HOME/.n8n-files/podolskiy"
# Лаба 3 берёт папку результата из переменной окружения
export LAB3_NOCODE_DIR="$HOME/.n8n-files/lab3"
# Часовой пояс расписаний (Пермь)
export GENERIC_TIMEZONE="Asia/Yekaterinburg"
# Узлы чтения/записи файлов работают только внутри этого каталога (n8n >= 1.9x)
export N8N_RESTRICT_FILE_ACCESS_TO="$HOME/.n8n-files"
# Пути к файлам в workflow строятся из $env.HOME (n8n 2.x по умолчанию запрещает $env в выражениях)
export N8N_BLOCK_ENV_ACCESS_IN_NODE=false
# Узел Execute Command (итоговый проект запускает пайплайн) в n8n 2.x по умолчанию выключен
export NODES_EXCLUDE="[]"

echo "Node $(node --version): $(command -v node)"
if command -v n8n >/dev/null 2>&1; then
  echo "n8n $(n8n --version 2>/dev/null || echo '?'): $(command -v n8n)"
  echo "Интерфейс: http://localhost:5678 (остановить — Ctrl+C)"
  exec n8n start
else
  echo "n8n в этом Node не установлен — запускаю через npx (первый раз скачивание займёт несколько минут)"
  echo "Интерфейс: http://localhost:5678 (остановить — Ctrl+C)"
  exec npx --yes n8n start
fi
