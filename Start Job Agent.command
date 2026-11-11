#!/bin/sh
# Double-click this file in Finder to start the web app and open it in your browser.
cd "$(dirname "$0")" || exit 1
./.venv/bin/python app.py
