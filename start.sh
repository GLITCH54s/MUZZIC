#!/usr/bin/env sh
# MUZZIC launcher for macOS/Linux: creates .venv on first run, then starts the app.
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python server.py "$@"