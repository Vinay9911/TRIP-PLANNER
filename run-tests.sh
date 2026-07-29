#!/usr/bin/env bash
# Run the test suite. No internet, database or API keys required.
set -euo pipefail
cd "$(dirname "$0")/backend"
PYTHONIOENCODING=utf-8 .venv/bin/python -m pytest -q
