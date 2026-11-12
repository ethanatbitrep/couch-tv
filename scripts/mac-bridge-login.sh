#!/bin/bash
# macOS: run the bridge in a Terminal window that opens by itself at every login
# (scripts/couch-tv-bridge.command), instead of as a background service. Use this when the
# background service cannot reach the TV ("No route to host" in /tmp/couch-tv-bridge.log).
#
#   scripts/mac-bridge-login.sh          set it up and start it now
#   scripts/mac-bridge-login.sh stop     stop it and remove it from Login Items
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CMD="$ROOT/scripts/couch-tv-bridge.command"
NAME=$(basename "$CMD")
cd "$ROOT"

# 1. take away the background service and anything else holding the bridge's port
launchctl bootout "gui/$(id -u)/app.couchtv.bridge" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/app.couchtv.bridge.plist"
pkill -f "CouchTVBridge.app/Contents/MacOS/couch-tv-bridge" 2>/dev/null || true
pkill -f "couch-tv-bridge.command" 2>/dev/null || true
pkill -f "$ROOT/.venv/bin/python bridge/server.py" 2>/dev/null || true
pkill -f ".venv/bin/python bridge/server.py" 2>/dev/null || true
sleep 2

osascript -e "tell application \"System Events\" to delete (every login item whose name is \"$NAME\")" >/dev/null 2>&1 || true
if [ "$1" = stop ]; then
  echo "Stopped, and removed from Login Items. Close the \"Couch TV bridge\" Terminal window if it is still open."
  exit 0
fi

# 2. open it at every login
if osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$CMD\", hidden:true}" >/dev/null 2>&1; then
  echo "Added to Login Items: it opens by itself every time you log in."
else
  echo "Could not add it to Login Items by itself. Add it by hand: System Settings, General, Login Items, +, then pick"
  echo "  $CMD"
fi

# 3. start it now, and check it sees the TV
open -a Terminal "$CMD"
KEY=$(.venv/bin/python -c 'import json; print((json.load(open("config.json")).get("bridge") or {}).get("pairing_key") or "")' 2>/dev/null || true)
PORT=$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2); PORT=${PORT:-5400}
for i in $(seq 1 15); do
  sleep 2
  OUT=$(curl -s -m 3 ${KEY:+-H "Authorization: Bearer $KEY"} "http://127.0.0.1:$PORT/api/tvs" || true)
  case "$OUT" in *'"connected": true'*) echo "TV: connected. All good."; exit 0 ;; esac
done
echo "TV: not seen yet. Is the TV on? Look at the \"Couch TV bridge\" Terminal window for the reason."
