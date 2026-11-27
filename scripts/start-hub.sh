#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"

PORT="${TEAM_HUB_PORT:-58767}"
WORKSPACE="./fixtures"
DATABASE=".team-hub/team.db"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --port|-p)
      [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
      PORT="$2"; shift 2 ;;
    --workspace|-w)
      [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
      WORKSPACE="$2"; shift 2 ;;
    --database|-d)
      [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
      DATABASE="$2"; shift 2 ;;
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

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "Python 3.11+ not found in PATH." >&2
  exit 1
fi

DATA_DIR="$ROOT_DIR/.team-hub"
mkdir -p "$DATA_DIR"
ENV_FILE="$DATA_DIR/.env"
PID_FILE="$DATA_DIR/hub-${PORT}.pid"
OUT_LOG="$DATA_DIR/hub-${PORT}.out.log"
ERR_LOG="$DATA_DIR/hub-${PORT}.err.log"
HUB_ENTRY="$ROOT_DIR/hub.py"

if [ -z "${TEAM_HUB_TOKEN:-}" ] && [ -f "$ENV_FILE" ]; then
  TEAM_HUB_TOKEN="$(awk -F= '$1=="TEAM_HUB_TOKEN"{sub(/^[^=]*=/,""); print; exit}' "$ENV_FILE")"
fi

if [ -z "${TEAM_HUB_TOKEN:-}" ]; then
  TEAM_HUB_TOKEN="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  umask 077
  printf 'TEAM_HUB_TOKEN=%s\n' "$TEAM_HUB_TOKEN" > "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "Generated TEAM_HUB_TOKEN and saved it to $ENV_FILE (mode 600)."
  echo "Export the same TEAM_HUB_TOKEN into your shell/GUI environment and restart clients so MCP tools can authenticate."
fi
export TEAM_HUB_TOKEN

check_health() {
  "$PYTHON_BIN" - "$PORT" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
port = int(sys.argv[1])
try:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as r:
        data = json.loads(r.read().decode('utf-8'))
        sys.exit(0 if data.get('status') == 'ok' else 1)
except Exception:
    sys.exit(1)
PY
}

is_this_checkout_pid() {
  _pid="$1"
  kill -0 "$_pid" 2>/dev/null || return 1
  _cmd="$(ps -p "$_pid" -o args= 2>/dev/null || true)"
  "$PYTHON_BIN" - "$_pid" "$HUB_ENTRY" "$PORT" "$_cmd" <<'PY' >/dev/null 2>&1
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
# Non-/proc Unix (e.g. macOS): match exact entry boundary and exact --port token without splitting entry spaces
if not re.search(r'(?:^|\s)' + re.escape(entry) + r'(?:\s|$)', ps_cmd):
    sys.exit(1)
m = re.search(r'(?:^|\s)--port(?:=|\s+)(\d+)(?:\s|$)', ps_cmd)
found_port = m.group(1) if m else '58767'
sys.exit(0 if found_port == port else 1)
PY
}

if check_health; then
  if [ -f "$PID_FILE" ]; then
    OLD_PID="$(tr -d '[:space:]' < "$PID_FILE")"
    case "$OLD_PID" in
      ''|*[!0-9]*) ;;
      *)
        if is_this_checkout_pid "$OLD_PID"; then
          echo "Team Hub from this checkout is already running on http://127.0.0.1:$PORT (PID $OLD_PID)."
          exit 0
        fi
        ;;
    esac
  fi
  echo "Port $PORT is already serving /health from another process or checkout. Refusing to start duplicate instance." >&2
  exit 1
fi

nohup "$PYTHON_BIN" "$HUB_ENTRY" \
  --workspace "$WORKSPACE" \
  --port "$PORT" \
  --database "$DATABASE" \
  >"$OUT_LOG" 2>"$ERR_LOG" </dev/null &
HUB_PID="$!"
printf '%s\n' "$HUB_PID" > "$PID_FILE"

i=0
while [ "$i" -lt 20 ]; do
  sleep 0.25
  if check_health; then
    echo "Team Hub started on http://127.0.0.1:$PORT (PID $HUB_PID)."
    echo "Logs: $OUT_LOG | $ERR_LOG"
    exit 0
  fi
  if ! kill -0 "$HUB_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Team Hub exited unexpectedly. Check $ERR_LOG:" >&2
    cat "$ERR_LOG" >&2 || true
    exit 1
  fi
  i=$((i + 1))
done

echo "Team Hub started (PID $HUB_PID) but /health did not respond within 5s." >&2
exit 1
