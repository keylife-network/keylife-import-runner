#!/bin/bash
# Rebuild keylife-import from GitHub and restart the container.
# Reads settings from .env beside this script.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="https://github.com/keylife-network/keylife-import-runner.git"
NAME="keylife-import"
PORT="${PORT:-8123}"

if [[ -f "$DIR/.env" ]]; then
  set -a; source "$DIR/.env"; set +a
else
  echo "Missing $DIR/.env — copy .env.example to .env and fill it in." >&2
  exit 1
fi

: "${WPAI_IMPORT_KEY:?WPAI_IMPORT_KEY is not set in .env}"

# Only forward optional overrides that are actually set: passing an empty
# value would override the app's default.
EXTRA=()
for v in DONE_PATTERNS PROCESSING_INTERVAL_SECONDS TRIGGER_INTERVAL_SECONDS \
         TRIGGER_ON_START SCHEDULER_ENABLED HTTP_TIMEOUT SITE_LABEL; do
  [[ -n "${!v:-}" ]] && EXTRA+=(-e "$v=${!v}")
done

echo "Building $NAME from $REPO ..."
docker build --pull --no-cache -t "$NAME" "$REPO"

echo "Restarting container ..."
docker rm -f "$NAME" 2>/dev/null || true
docker run -d --name "$NAME" --restart unless-stopped -p "$PORT:8080" \
  -v "$DIR/data:/data" \
  -e WPAI_BASE_URL="${WPAI_BASE_URL:-https://www.keylife.org/wp-load.php}" \
  -e WPAI_IMPORT_KEY="$WPAI_IMPORT_KEY" \
  -e WPAI_IMPORT_ID="${WPAI_IMPORT_ID:-13}" \
  -e ACCESS_TOKEN="${ACCESS_TOKEN:-}" \
  "${EXTRA[@]}" \
  "$NAME"

sleep 2
echo -n "Health: "; curl -fsS "http://localhost:$PORT/healthz" || echo "FAILED"
echo
