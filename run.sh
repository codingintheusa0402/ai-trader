#!/usr/bin/env bash
# Start the engine. Opens http://127.0.0.1:8764 (or the port in AITRADER_PORT). Ctrl+C stops it.
# Dry-run is the default: nothing reaches your broker until you do every step in README "실전 전환".
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Run ./setup.sh first."; exit 1; }
if [ "${1:-}" = "--window" ]; then
  exec .venv/bin/python -m app.main
fi
export AITRADER_HEADLESS=1
exec .venv/bin/python -m app.main
