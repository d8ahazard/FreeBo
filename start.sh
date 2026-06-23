#!/usr/bin/env bash
# FreeBo launcher (Linux / macOS / Pi). Clone the repo, run:  ./start.sh
# Thin wrapper around scripts/bootstrap.py (creates venv, installs deps, builds UI, starts the server).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ is required and was not found on PATH. Install it and retry." >&2
  exit 1
fi

exec "$PY" "$ROOT/scripts/bootstrap.py" "$@"
