#!/usr/bin/env bash
# Local n8n for the M2 migration workflows (n8n/workflows/*.json).
#
#   n8n/m2-n8n.sh install     npm install of n8n in ~/.local/share/n8n-app (once)
#   n8n/m2-n8n.sh start       n8n on http://127.0.0.1:5678 (the next free port when taken), data in ~/.local/share/n8n-data
#   n8n/m2-n8n.sh stop | status | logs
#   n8n/m2-n8n.sh import      import (or update) and publish the workflows of n8n/workflows
#   n8n/m2-n8n.sh run-report  start the "run + local AI report" flow through its webhook (n8n must run, flow active)
#
# n8n reads the migration UI's API token (bin/m2ctl.sh token) and Ollama's address from the environment set here;
# it listens on 127.0.0.1 only and may write files only under runs/.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
APP="${N8N_APP:-$HOME/.local/share/n8n-app}"
export N8N_USER_FOLDER="${N8N_USER_FOLDER:-$HOME/.local/share/n8n-data}"
BIN="$APP/node_modules/.bin/n8n"
LOG="$HERE/logs/n8n.log"
PORTFILE="$N8N_USER_FOLDER/.m2_port"
PIDFILE="$N8N_USER_FOLDER/.m2_pid"
mkdir -p "$HERE/logs" "$N8N_USER_FOLDER"
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

free_port() { local p="${1:-5678}"; while ss -ltn "sport = :$p" | grep -q LISTEN; do p=$((p + 1)); done; echo "$p"; }
port() { [ -f "$PORTFILE" ] && cat "$PORTFILE" || echo "${N8N_PORT:-5678}"; }
url() { echo "http://127.0.0.1:$(port)"; }
healthy() { curl -fsS -m 3 "$(url)/healthz" >/dev/null 2>&1; }

env_n8n() {
  export N8N_HOST=127.0.0.1 N8N_LISTEN_ADDRESS=127.0.0.1 N8N_PROTOCOL=http N8N_SECURE_COOKIE=false
  export N8N_DIAGNOSTICS_ENABLED=false N8N_PERSONALIZATION_ENABLED=false N8N_VERSION_NOTIFICATIONS_ENABLED=false
  export N8N_BLOCK_ENV_ACCESS_IN_NODE=false N8N_RUNNERS_ENABLED=true
  export N8N_RESTRICT_FILE_ACCESS_TO="$HERE/runs" GENERIC_TIMEZONE="${GENERIC_TIMEZONE:-Asia/Dubai}"
  export M2_UI_URL="${M2_UI_URL:-http://127.0.0.1:${M2_PORT:-8796}}" M2_REPORT_DIR="$HERE/runs"
  export M2_API_TOKEN="${M2_API_TOKEN:-$("$HERE/bin/m2ctl.sh" token)}"
  export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}" OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.1:8b}"
  export N8N_RUNNERS_ALLOWED_ENV="${N8N_RUNNERS_ALLOWED_ENV:-M2_UI_URL,M2_API_TOKEN,M2_SOURCE,M2_REPORT_DIR,OLLAMA_URL,OLLAMA_MODEL}"
}

case "${1:-status}" in
  install)
    mkdir -p "$APP" && cd "$APP" && { [ -f package.json ] || npm init -y >/dev/null; } &&
      npm install n8n@latest --omit=dev --no-audit --no-fund ;;
  start)
    [ -x "$BIN" ] || { echo "n8n is not installed: $0 install"; exit 1; }
    if healthy; then echo "Already running on $(url)"; exit 0; fi
    p="$(free_port "${N8N_PORT:-5678}")"; echo "$p" > "$PORTFILE"; export N8N_PORT="$p"
    env_n8n
    echo "Starting n8n on $(url)"
    setsid nohup "$BIN" start >>"$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 90); do healthy && { echo "Up: $(url)"; exit 0; }; sleep 1; done
    echo "Did not come up: last log lines:"; tail -25 "$LOG"; exit 1 ;;
  stop)
    # n8n renames its process ("n8n start"): find it by the pid file, else by the port it listens on.
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    kill -0 "$pid" 2>/dev/null || pid="$(ss -ltnpH "sport = :$(port)" | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
    [ -n "$pid" ] || { echo "Not running"; exit 0; }
    kill -- "-$pid" 2>/dev/null || kill "$pid"    # its process group: n8n and its task runner
    for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || { rm -f "$PIDFILE"; echo "Stopped"; exit 0; }; sleep 0.5; done
    kill -9 "$pid" 2>/dev/null; rm -f "$PIDFILE"; echo "Killed" ;;
  status) if healthy; then echo "Running on $(url)"; else echo "Not running"; exit 3; fi ;;
  logs) tail -50 "$LOG" ;;
  import)
    env_n8n
    for f in "$HERE"/n8n/workflows/*.json; do
      echo "Importing $(basename "$f")"; "$BIN" import:workflow --input="$f" || exit 1
      id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$f")"
      "$BIN" publish:workflow --id="$id" >/dev/null 2>&1 && echo "  published $id (schedule and production webhook on)"
    done
    echo "Restart n8n ($0 stop; $0 start) if it is running, then open $(url)" ;;
  run-report)
    curl -fsS -m 900 -X POST "$(url)/webhook/m2-run" -H 'Content-Type: application/json' -d "${2:-{\}}"; echo ;;
  *) echo "Usage: $0 install|start|stop|status|logs|import|run-report [json]"; exit 2 ;;
esac
