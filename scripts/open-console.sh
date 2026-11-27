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

ENV_FILE="$ROOT_DIR/.team-hub/.env"
if [ -z "${TEAM_HUB_TOKEN:-}" ] && [ -f "$ENV_FILE" ]; then
  TEAM_HUB_TOKEN="$(awk -F= '$1=="TEAM_HUB_TOKEN"{sub(/^[^=]*=/,""); print; exit}' "$ENV_FILE")"
fi
if [ -z "${TEAM_HUB_TOKEN:-}" ]; then
  echo "TEAM_HUB_TOKEN is missing. Start Hub via scripts/start-hub.sh first." >&2
  exit 1
fi

PYTHON_BIN="python3"
command -v python3 >/dev/null 2>&1 || PYTHON_BIN="python"
ENCODED="$(TEAM_HUB_TOKEN="$TEAM_HUB_TOKEN" "$PYTHON_BIN" -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["TEAM_HUB_TOKEN"], safe=""))')"
CLEAN_URL="http://127.0.0.1:$PORT/manage"
AUTH_URL="$CLEAN_URL#token=$ENCODED"

if command -v open >/dev/null 2>&1; then
  open "$AUTH_URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$AUTH_URL" >/dev/null 2>&1 &
else
  echo "No browser launcher (open/xdg-open) detected."
  echo "Open $CLEAN_URL in your browser and paste TEAM_HUB_TOKEN manually (token not printed for safety; check $ENV_FILE or your environment)."
fi
