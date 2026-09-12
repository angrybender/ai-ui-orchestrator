#!/usr/bin/env bash
set -e

if [[ -x "$(dirname "$0")/dev-venv/bin/uvicorn" ]]; then
  exec "$(dirname "$0")/dev-venv/bin/uvicorn" main:app --host 0.0.0.0 --port 8000 --reload
fi

exec uvicorn main:app --host 0.0.0.0 --port 8000 --reload