#!/bin/bash
# macOS: runs the bridge in a Terminal window, restarting it if it ever stops. Terminal already has
# Local Network permission, which a background (launchd) Python can be refused with no prompt at all
# ("No route to host", #42). scripts/mac-bridge-login.sh opens this at every login.
# caffeinate -i keeps the Mac from idle sleep while the bridge runs.
cd "$(dirname "$0")/.." || exit 1
printf '\033]0;Couch TV bridge\007'
echo "Couch TV bridge. Leave this window open (you can minimize it)."
while true; do
  caffeinate -i .venv/bin/python bridge/server.py
  echo "The bridge stopped. Starting it again in 5 seconds..."
  sleep 5
done
