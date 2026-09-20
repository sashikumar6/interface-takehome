#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [[ ! -x .venv/bin/python ]]; then
  echo "missing .venv; run: uv sync --all-extras" >&2
  exit 2
fi

.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy src/computer_use
.venv/bin/pytest
.venv/bin/python -m computer_use.cli schema export --output artifacts/schemas
.venv/bin/python scripts/scan_secrets.py
.venv/bin/python scripts/check_submission.py
