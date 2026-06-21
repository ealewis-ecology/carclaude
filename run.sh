#!/usr/bin/env bash
# Start the carclaude server. cloudflared connects to it on 127.0.0.1.
set -euo pipefail
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
set -a; [ -f .env ] && . ./.env; set +a
exec python -m uvicorn server.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8787}"
