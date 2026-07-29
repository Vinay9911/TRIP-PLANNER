#!/usr/bin/env bash
# Start the web interface. Run start-backend.sh first, in another terminal.
set -euo pipefail
cd "$(dirname "$0")/frontend"

[ -d node_modules ] || npm install
if [ ! -f ".env.local" ]; then
  echo "No .env.local found. Copy frontend/.env.example to frontend/.env.local first."
  exit 1
fi

echo "Web interface: http://localhost:3000"
npm run dev
