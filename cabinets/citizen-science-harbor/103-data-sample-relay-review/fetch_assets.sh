#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
GP_ASSET_URL="${RELAY_GP_ASSET_URL:-http://49.233.162.81:8788/all_sample_gp.tar}"
SCATTER_ASSET_URL="${RELAY_SCATTER_ASSET_URL:-http://49.233.162.81:8788/all_sample_scatter.tar}"

ensure_asset() {
  local name="$1"
  local asset_url="$2"

  if find "$name" -maxdepth 1 -type f -name '*.png' 2>/dev/null | grep -q .; then
    echo "$name already exists; skip download."
    return
  fi

  echo "Downloading $name assets..."
  echo "$asset_url"
  curl -fL "$asset_url" -o "$name.tar"

  echo "Extracting $name.tar..."
  tar -xf "$name.tar"

  COUNT="$(find "$name" -maxdepth 1 -type f -name '*.png' | wc -l | tr -d ' ')"
  if [ "$COUNT" = "0" ]; then
    echo "No PNG files found in $name after extraction." >&2
    exit 1
  fi

  echo "Ready: $COUNT $name images."
}

ensure_asset all_sample_gp "$GP_ASSET_URL"
ensure_asset all_sample_scatter "$SCATTER_ASSET_URL"
