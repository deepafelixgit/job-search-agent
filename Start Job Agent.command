#!/bin/sh
# Double-click this file in Finder to start the web app and open it in your browser.
# If the app is already running (perhaps with older code), that copy is stopped first, so the app
# always runs the latest code. A background Claude Code run that is writing applications keeps going.
cd "$(dirname "$0")" || exit 1
for pid in $(lsof -ti tcp:5055 -sTCP:LISTEN 2>/dev/null); do
  if ps -p "$pid" -o command= | grep -q "app.py"; then
    echo "Stopping the app that was already running (process $pid)..."
    kill "$pid"
  fi
done
sleep 1
./.venv/bin/python app.py
