#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x .venv/bin/uvicorn ]; then
  echo "backend/.venv is missing or incomplete. Create it first:" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

args=(app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}")
if [ "${RELOAD:-1}" != "0" ]; then
  args+=(--reload)
fi

exec .venv/bin/uvicorn "${args[@]}"
