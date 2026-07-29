#!/usr/bin/env bash
# Start the API. Creates the virtualenv and installs dependencies on first run.
set -euo pipefail
cd "$(dirname "$0")/backend"

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtual environment (first run only)..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e ".[dev]"
fi

if [ ! -f "../.env" ]; then
  echo "No .env file found. Copy .env.example to .env and fill it in first."
  exit 1
fi

echo "API:  http://localhost:8000"
echo "Docs: http://localhost:8000/docs"
.venv/bin/python run.py --port 8000
