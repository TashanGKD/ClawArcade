#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
PORT="${PORT:-8788}"
export RELAY_PUBLIC_BASE_URL="${RELAY_PUBLIC_BASE_URL:-http://127.0.0.1:${PORT}}"

has_png_asset() {
  local name="$1"
  find "$name" -maxdepth 1 -type f -name '*.png' 2>/dev/null | grep -q .
}

if ! has_png_asset all_sample_gp || ! has_png_asset all_sample_scatter; then
  ./fetch_assets.sh
fi
python3 ./relay_server.py --host 127.0.0.1 --port "$PORT"
