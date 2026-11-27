#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
PORT="${TEAM_HUB_PORT:-58767}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --port|-p)
      [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
      PORT="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$PORT" in
  ''|*[!0-9]*) echo "Invalid port: $PORT (expected integer 1-65535)" >&2; exit 2 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "Invalid port: $PORT (expected integer 1-65535)" >&2
  exit 2
fi

PYTHON_BIN="python3"
command -v python3 >/dev/null 2>&1 || PYTHON_BIN="python"

HUB_ENTRY="$ROOT_DIR/hub.py"
PID_FILE="$ROOT_DIR/.team-hub/hub-${PORT}.pid"
if [ ! -f "$PID_FILE" ] && [ "$PORT" -eq 58767 ] && [ -f "$ROOT_DIR/.team-hub/hub.pid" ]; then
  PID_FILE="$ROOT_DIR/.team-hub/hub.pid"
fi

HEALTH="$("$PYTHON_BIN" - "$PORT" <<'PY' 2>/dev/null || echo 'unreachable'
import json, sys, urllib.request
port = int(sys.argv[1])
try:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as r:
        print(json.loads(r.read().decode('utf-8')).get('status', 'unknown'))
except Exception:
    print('unreachable')
PY
)"

if [ "$HEALTH" != "ok" ]; then
  echo "Status: STOPPED (http://127.0.0.1:$PORT health=$HEALTH)"
  exit 1
fi

PID_NOTE="unknown (no local PID file for this checkout)"
if [ -f "$PID_FILE" ]; then
  PID="$(tr -d '[:space:]' < "$PID_FILE")"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    CMDLINE="$(ps -p "$PID" -o args= 2>/dev/null || true)"
    if "$PYTHON_BIN" - "$PID" "$HUB_ENTRY" "$PORT" "$CMDLINE" <<'PY' >/dev/null 2>&1
import os, re, sys
pid, entry, port, ps_cmd = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
proc_cmdline = f'/proc/{pid}/cmdline'
if os.path.isfile(proc_cmdline):
    try:
        raw = open(proc_cmdline, 'rb').read()
        argv = [p.decode('utf-8', 'surrogateescape') for p in raw.split(b'\x00') if p]
    except OSError:
        sys.exit(1)
    if entry not in argv:
        sys.exit(1)
    found_port = '58767'
    for i, tok in enumerate(argv):
        if tok == '--port' and i + 1 < len(argv):
            found_port = argv[i + 1]
        elif tok.startswith('--port='):
            found_port = tok.split('=', 1)[1]
    sys.exit(0 if found_port == port else 1)
if not re.search(r'(?:^|\s)' + re.escape(entry) + r'(?:\s|$)', ps_cmd):
    sys.exit(1)
m = re.search(r'(?:^|\s)--port(?:=|\s+)(\d+)(?:\s|$)', ps_cmd)
found_port = m.group(1) if m else '58767'
sys.exit(0 if found_port == port else 1)
PY
    then
      PID_NOTE="$PID (this checkout)"
    else
      PID_NOTE="$PID (different process/checkout)"
    fi
  else
    PID_NOTE="$PID (stale PID file)"
  fi
fi

echo "Status: RUNNING"
echo "Endpoint: http://127.0.0.1:$PORT (health=$HEALTH)"
echo "PID: $PID_NOTE"
