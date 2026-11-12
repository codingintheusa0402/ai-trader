#!/usr/bin/env bash
# One-time setup: Python venv + packages + a private .env + the state folder. Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
fi
[ -n "$PY" ] || { echo "Python 3.11 or newer is needed (brew install python / apt install python3)."; exit 1; }
echo "Python: $($PY --version)"

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
if [ "${DESKTOP:-0}" = "1" ]; then .venv/bin/python -m pip install --quiet -r requirements-desktop.txt; fi

if [ ! -f .env ]; then
  umask 077
  cp .env.example .env
  echo "Created .env from .env.example (private, chmod 600). Broker keys are optional for dry-run."
fi
chmod 600 .env
mkdir -p state/app

if command -v claude >/dev/null 2>&1; then
  echo "LLM: Claude Code CLI found (news scoring + Live chat use it)."
else
  echo "LLM: 'claude' CLI not found. The engine still runs; set AITRADER_LLM_CMD in .env (e.g. codex exec -) or =off."
fi
echo
echo "Done. Start in dry-run (no real orders):  ./run.sh"
