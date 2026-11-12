#!/usr/bin/env bash
# Run all 6 services WITHOUT Docker (for development). Needs Python 3.10+.  Usage: ./run_local.sh
# Cart/orders use in-memory storage here (no Redis needed). Open http://localhost:8000 after start.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
for s in gateway catalog search chatbot cart order; do pip install -q -r services/$s/requirements.txt; done
export FRONTEND_DIR="$PWD/frontend"
export CATALOG_URL=http://localhost:8001 SEARCH_URL=http://localhost:8002 CHATBOT_URL=http://localhost:8003 \
       CART_URL=http://localhost:8004 ORDER_URL=http://localhost:8005
unset REDIS_URL
pids=()
trap 'kill ${pids[@]} 2>/dev/null' EXIT INT TERM
start() { (cd services/$1 && uvicorn app.main:app --port $2 --log-level warning) & pids+=($!); echo "started $1 on :$2"; }
start catalog 8001; start search 8002; start chatbot 8003; start cart 8004; start order 8005; start gateway 8000
echo "Open http://localhost:8000  (page + API)   |   health of all services: http://localhost:8000/api/status"
wait
