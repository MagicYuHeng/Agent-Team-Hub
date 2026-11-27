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
if [ ! -f "$PID_FILE" ]; then
  echo "No PID file found for port $PORT ($PID_FILE). Team Hub may not be running."
  exit 0
fi

PID="$(tr -d '[:space:]' < "$PID_FILE")"
case "$PID" in
  ''|*[!0-9]*)
    echo "Ignoring invalid PID in $PID_FILE." >&2
    rm -f "$PID_FILE"
    exit 0
    ;;
esac

if ! kill -0 "$PID" 2>/dev/null; then
  echo "No running process for PID $PID. Cleaned stale PID file."
  rm -f "$PID_FILE"
  exit 0
fi

CMDLINE="$(ps -p "$PID" -o args= 2>/dev/null || true)"
if ! "$PYTHON_BIN" - "$PID" "$HUB_ENTRY" "$PORT" "$CMDLINE" <<'PY' >/dev/null 2>&1
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
  echo "PID $PID does not match this checkout's Team Hub on port $PORT ($CMDLINE). Refusing to stop." >&2
  exit 1
fi

kill "$PID"
i=0
while [ "$i" -lt 20 ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Team Hub (PID $PID) stopped cleanly."
    exit 0
  fi
  sleep 0.2
  i=$((i + 1))
done

echo "Team Hub (PID $PID) did not exit after SIGTERM." >&2
exit 1
