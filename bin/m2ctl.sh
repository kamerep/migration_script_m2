#!/usr/bin/env bash
# Control of the M2 migration UI, used by Portwatch (start, stop, actions) and by hand.
#
#   bin/m2ctl.sh start | stop | restart | status
#   bin/m2ctl.sh test            unit tests
#   bin/m2ctl.sh deploy          tests, then restart on the new code, then health check (stops at the first failure)
#   bin/m2ctl.sh sample          sample exports + a terminal run of the migration on them
#   bin/m2ctl.sh migrate [dir]   terminal run on a folder (default: the last folder chosen in the UI)
#   bin/m2ctl.sh last            totals of the last run
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"
PORT="${M2_PORT:-8796}"
LOG="$HERE/logs/m2_ui.log"
URL="http://127.0.0.1:$PORT"
mkdir -p "$HERE/logs"
cd "$HERE" || exit 1

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
  *) echo "Usage: $0 start|stop|restart|status|test|deploy|sample|migrate [dir]|last"; exit 2 ;;
esac
