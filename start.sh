#!/bin/bash
# Start script for LunaRecycle LR.py interface

cd "$(dirname "$0")"

# Activate local virtual environment when present.
if [ -f "./venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . "./venv/bin/activate"
elif [ -f "./.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  . "./.venv/bin/activate"
fi

# Prefer project virtual environment if present.
if [ -x "./venv/bin/python" ]; then
  PYTHON_BIN="./venv/bin/python"
elif [ -x "./.venv/bin/python" ]; then
  PYTHON_BIN="./.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "python3 not found. Please install Python 3."
  exit 1
fi

# Run GUI when a display is available; otherwise run console temperature monitor.
if [ -z "${DISPLAY:-}" ]; then
  echo "No DISPLAY detected. Starting console temperature monitor instead of GUI."
  exec "$PYTHON_BIN" temp_monitor.py
fi

export CTR_TARGET="${CTR_TARGET:-Hardware}"
export CAN_BUS_NAME="${CAN_BUS_NAME:-CanBus}"
exec "$PYTHON_BIN" LR.py
