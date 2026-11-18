#!/usr/bin/env bash
# Control of the M2 migration UI, used by Portwatch (start, stop, actions) and by hand.
#
#   bin/m2ctl.sh start | stop | restart | status
#   bin/m2ctl.sh test            unit tests
#   bin/m2ctl.sh deploy          tests, then restart on the new code, then health check (stops at the first failure)
#   bin/m2ctl.sh sample          sample exports + a terminal run of the migration on them
#   bin/m2ctl.sh migrate [dir]   terminal run on a folder (default: the last folder chosen in the UI)
#   bin/m2ctl.sh last            totals of the last run
#   bin/m2ctl.sh ai status|suggest|documents|summary|all [run]   AI assistant (default run: the last one)
#   bin/m2ctl.sh token           the stable API token for n8n / the MCP server
#   bin/m2ctl.sh mcp             MCP server on stdio (for Claude Code)
#   bin/m2ctl.sh audit [run]     UI audit of the running page: layout at 320-1440 px, structure, motion, axe, contrast in every palette
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"
PORT="${M2_PORT:-8796}"
LOG="$HERE/logs/m2_ui.log"
URL="http://127.0.0.1:$PORT"
mkdir -p "$HERE/logs"
cd "$HERE" || exit 1
# Secrets (ANTHROPIC_API_KEY, M2_API_TOKEN) stay in .env, never in git.
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

pid() { pgrep -f "$HERE/m2_ui.py --port $PORT" | head -1; }
healthy() { curl -fsS -m 3 "$URL/health" >/dev/null 2>&1; }

start() {
  if healthy; then echo "Already running on $URL"; return 0; fi
  echo "Starting the M2 migration UI on $URL"
  setsid nohup "$PY" "$HERE/m2_ui.py" --port "$PORT" --no-browser >>"$LOG" 2>&1 < /dev/null &
  for _ in $(seq 1 20); do healthy && { echo "Up: $URL"; return 0; }; sleep 0.5; done
  echo "Did not come up: last log lines:"; tail -20 "$LOG"; return 1
}
stop() {
  local p; p="$(pid)"
  if [ -z "$p" ]; then echo "Not running"; return 0; fi
  echo "Stopping pid $p"; kill "$p"
  for _ in $(seq 1 20); do kill -0 "$p" 2>/dev/null || { echo "Stopped"; return 0; }; sleep 0.25; done
  kill -9 "$p" 2>/dev/null; echo "Killed"
}
status() {
  if healthy; then echo "Running on $URL (pid $(pid))"; curl -fsS -m 3 "$URL/health"; echo; else echo "Not running"; return 3; fi
}
tests() { "$PY" -m unittest discover -s "$HERE/tests" -v; }
last() {
  local run; run="$(ls -1d "$HERE"/runs/*/ 2>/dev/null | sort | tail -1)"
  [ -n "$run" ] || { echo "No run yet"; return 0; }
  echo "Last run: $(basename "$run")"
  "$PY" - "$run/summary.json" <<'PYEOF'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except OSError:
    sys.exit("The run did not finish (no summary.json): see its run.log")
print("Source:", s["source"])
for sheet in s["sheets"].values():
    if sheet["import"] or sheet["review"]:
        print(f"  {sheet['title']:16} {sheet['import']:5} to import  {sheet['review']:5} to review")
print("Journal:", ", ".join(f"{k} {v}" for k, v in s["levels"].items()))
PYEOF
}

# UI audit (Interface Craft playbook I2): needs playwright-core and a Chromium; axe-core is used when found.
audit() {
  local core chrome axe run sections
  core="${PW_CORE:-$(ls -d "$HOME"/.local/share/*/node_modules/playwright-core "$(npm root -g 2>/dev/null)"/playwright-core 2>/dev/null | head -1)}"
  chrome="${CHROME_PATH:-$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux64/chrome 2>/dev/null | sort | tail -1)}"
  [ -n "$chrome" ] || chrome="$(command -v chromium || command -v google-chrome || command -v brave-browser || true)"
  axe="${AXE_PATH:-$(ls "$HOME"/.npm/_npx/*/node_modules/axe-core/axe.min.js 2>/dev/null | head -1)}"
  [ -n "$core" ] && [ -n "$chrome" ] || { echo "Needs playwright-core (PW_CORE) and Chromium (CHROME_PATH): npm i -g playwright-core && npx playwright install chromium"; return 2; }
  healthy || { echo "The UI is not running: bin/m2ctl.sh start"; return 2; }
  run="${1:-$(ls -1d "$HERE"/runs/*/ 2>/dev/null | sort | tail -1 | xargs -r basename)}"
  sections="overview decide ai journal records files review values finance settings docs"
  local pages="" s; for s in $sections; do pages="$pages,/?run=$run#$s"; done
  mkdir -p "$HERE/logs/ui-audit"
  local rc=0
  for scheme in light dark; do
    echo "== layout, structure, motion, axe ($scheme)"
    PW_CORE="$core" CHROME_PATH="$chrome" AXE_PATH="$axe" BASE_URL="$URL" PAGES="${pages#,}" COLOR_SCHEME="$scheme" \
      OUT="$HERE/logs/ui-audit/$scheme" node "$HERE/tools/ui_audit.mjs" > "$HERE/logs/ui-audit/$scheme.txt" 2>&1 || rc=1
    grep -v ' ok$' "$HERE/logs/ui-audit/$scheme.txt"
  done
  echo "== contrast, every palette, light and dark"
  PW_CORE="$core" CHROME_PATH="$chrome" BASE_URL="$URL" RUN="$run" node "$HERE/tools/ui_contrast.mjs" || rc=1
  return $rc
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  test) tests ;;
  deploy)
    echo "== 1/3 tests"; tests || { echo "Tests failed: the running UI is left as it is."; exit 1; }
    echo "== 2/3 restart on the new code"; stop; start || exit 1
    echo "== 3/3 health"; status ;;
  sample)
    "$PY" "$HERE/make_sample_m2.py" && "$PY" "$HERE/migration_script_m2_odoo.py" "$HERE/sample_m2" \
      "$HERE/runs/cli-sample" --quiet ;;
  migrate)
    dir="${2:-$("$PY" -c "import json;print(json.load(open('$HERE/.m2_ui.json')).get('source',''))" 2>/dev/null)}"
    [ -n "$dir" ] || { echo "Give the folder: bin/m2ctl.sh migrate <folder>"; exit 1; }
    "$PY" "$HERE/migration_script_m2_odoo.py" "$dir" "$HERE/runs/cli-$(date +%Y%m%d-%H%M%S)" --quiet ;;
  last) last ;;
  ai)
    cmd="${2:-status}"; run="${3:-$(ls -1d "$HERE"/runs/*/ 2>/dev/null | sort | tail -1)}"
    if [ "$cmd" = status ]; then "$PY" "$HERE/m2_ai.py" status; else "$PY" "$HERE/m2_ai.py" "$cmd" "$run"; fi ;;
  token) "$PY" -c "import sys; sys.argv=['x']; import m2_ui; print(m2_ui.API_TOKEN)" ;;
  mcp) exec "$PY" "$HERE/m2_mcp.py" ;;
  audit) audit "${2:-}" ;;
  *) echo "Usage: $0 start|stop|restart|status|test|deploy|sample|migrate [dir]|last|ai <cmd> [run]|token|mcp|audit [run]"; exit 2 ;;
esac
