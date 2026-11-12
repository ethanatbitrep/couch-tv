#!/bin/bash
# macOS: run the bridge in the background (a LaunchAgent) from a small app bundle, CouchTVBridge.app,
# so macOS's Local Network privacy lets it reach the TV. A Python started by launchd on its own can
# be refused ("No route to host", or the TV just never seen) even with Python switched on under
# System Settings, Privacy and Security, Local Network (#42; seen again on a Mac mini with
# Homebrew's Python 3.14). The bundle gives the interpreter its own identity, and launchd starts
# it through `open` (LaunchServices), the way a double click does: only then does macOS show the
# "find devices on your local network" prompt for it. Started directly by launchd, the bundle got
# "No route to host" with no prompt and no entry under Local Network (the same Mac mini).
#
#   scripts/mac-bridge-service.sh          set it up (or again after a Python upgrade), start it
#   scripts/mac-bridge-service.sh stop     stop it and take the LaunchAgent away
#
# Needs the repo's .venv (README step 2). Logs: /tmp/couch-tv-bridge.log
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
LABEL=app.couchtv.bridge
AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
APP="$ROOT/build/CouchTVBridge.app"
EXE="$APP/Contents/MacOS/couch-tv-bridge"
LOG=/tmp/couch-tv-bridge.log
DOMAIN="gui/$(id -u)"

[ "$(uname)" = Darwin ] || { echo "This is for macOS only."; exit 1; }

# bootout returns before launchd has finished taking the old service away, and a bootstrap in
# that window fails with "Bootstrap failed: 5: Input/output error" (seen on a Mac mini): wait for it
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
for i in $(seq 1 20); do launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break; sleep 0.5; done
pkill -f "$EXE" 2>/dev/null || true   # the app outlives the `open` that launchd watches
if [ "$1" = stop ]; then
  rm -f "$AGENT"
  echo "Stopped. The bridge no longer runs in the background."
  exit 0
fi

VPY="$ROOT/.venv/bin/python"
[ -x "$VPY" ] || { echo "No .venv yet: run README step 2 first."; exit 1; }
REAL=$("$VPY" -c 'import os, sys; print(os.path.realpath(sys.executable))')
BASE=$("$VPY" -c 'import sys; print(sys.base_prefix)')
SITE=$("$VPY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')

# the bundle: a copy of the interpreter under its own name and identity
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$REAL" "$EXE"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>Couch TV bridge</string>
  <key>CFBundleExecutable</key><string>couch-tv-bridge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Couch TV talks to your TV on your home network.</string>
  <key>LSEnvironment</key>
  <dict>
    <key>PYTHONHOME</key><string>$BASE</string>
    <key>PYTHONPATH</key><string>$SITE</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF
codesign -s - --force "$APP" >/dev/null 2>&1 || { echo "codesign failed: install the developer tools (xcode-select --install) and run this again."; exit 1; }

# check the copy really runs as this Python before handing it to launchd
PYTHONHOME="$BASE" PYTHONPATH="$SITE" "$EXE" -c 'import aiohttp' \
  || { echo "The bundled Python could not start ($REAL). Nothing was changed in the background."; exit 1; }

mkdir -p "$(dirname "$AGENT")"
cat > "$AGENT" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-W</string>
    <string>-g</string>
    <string>-n</string>
    <string>--stdout</string><string>$LOG</string>
    <string>--stderr</string><string>$LOG</string>
    <string>-a</string><string>$APP</string>
    <string>--args</string><string>$ROOT/bridge/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/tmp/couch-tv-bridge-launch.log</string>
  <key>StandardErrorPath</key><string>/tmp/couch-tv-bridge-launch.log</string>
</dict>
</plist>
EOF
rm -f "$LOG" /tmp/couch-tv-bridge-launch.log
ok=""
for i in 1 2 3 4 5; do
  if launchctl bootstrap "$DOMAIN" "$AGENT" 2>/dev/null; then ok=1; break; fi
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 && { ok=1; break; }   # it is in after all
  sleep 2
done
[ -n "$ok" ] || { echo "macOS would not start it. Run this again in a minute; if it still fails, send the output of: plutil -lint \"$AGENT\""; exit 1; }
echo "Started. If macOS asks to let \"Couch TV bridge\" find devices on your local network, click Allow."
sleep 6
launchctl print "$DOMAIN/$LABEL" | grep -m1 "state =" || true

# can it see the TVs?
cd "$ROOT"
KEY=$("$VPY" -c 'import json; print((json.load(open("config.json")).get("bridge") or {}).get("pairing_key") or "")' 2>/dev/null || true)
PORT=$(grep -E '^PORT=' .env 2>/dev/null | cut -d= -f2); PORT=${PORT:-5400}
for i in $(seq 1 20); do
  OUT=$(curl -s -m 5 ${KEY:+-H "Authorization: Bearer $KEY"} "http://127.0.0.1:$PORT/api/tvs" || true)
  case "$OUT" in *'"connected": true'*) break ;; esac
  [ "$i" = 3 ] && echo "Waiting for the TV (click Allow if macOS asks)..."
  sleep 2
done
case "$OUT" in
  *'"connected": true'*) echo "TV: connected. All good." ;;
  *'"connected": false'*) echo "TV: not seen yet. Make sure the TV is on, click Allow if macOS asked, then run this again. Last line of $LOG:"; tail -1 "$LOG" 2>/dev/null || true ;;
  *) echo "The bridge did not answer yet. Last lines of $LOG:"; tail -5 "$LOG" 2>/dev/null || true ;;
esac
