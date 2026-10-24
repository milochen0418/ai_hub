#!/bin/bash

# Auto-load project .env so its settings (e.g. CAD_BUILD_TIMEOUT) are set
# without the caller having to remember `set -a; source .env; set +a`.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
  echo "Loading $SCRIPT_DIR/.env"
  set -a
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/.env"
  set +a
fi

echo "Checking for processes on ports 3000 and 8000..."

# Find PIDs using ports 3000 and 8000
PIDS=$(lsof -ti:3000,8000)

if [ -n "$PIDS" ]; then
  echo "Killing processes: $PIDS"
  kill -9 $PIDS
  echo "Processes killed."
else
  echo "No processes found on ports 3000 or 8000."
fi

echo "Starting Reflex app with args: $@"
poetry run reflex run "$@"
