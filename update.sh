#!/bin/bash
# Rebuild keylife-import from GitHub and restart the container.
# Reads WPAI_IMPORT_KEY / ACCESS_TOKEN etc. from .env beside this script.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="https://github.com/keylife-network/keylife-import-runner.git"
NAME="keylife-import"
PORT="${PORT:-8123}"

# Only forward optional overrides that are actually set.
EXTRA=()

if [[ -f "$DIR/.env" ]]; then
  set -a; source "$DIR/.env"; set +a
else
  echo "Missing $DIR/.env — copy .env.example to .env and fill it in." >&2
  exit 1
fi

: "${WPAI_IMPORT_KEY:?WPAI_IMPORT_KEY is not set in .env}"

[[ -n "${DONE_PATTERNS:-}" ]] && EXTRA+=(-e "DONE_PATTERNS=$DONE_PATTERNS")
[[ -n "${MAX_SECONDS:-}"   ]] && EXTRA+=(-e "MAX_SECONDS=$MAX_SECONDS")
[[ -n "${MAX_ATTEMPTS:-}"  ]] && EXTRA+=(-e "MAX_ATTEMPTS=$MAX_ATTEMPTS")

echo "Building $NAME from $REPO ..."
docker build --pull --no-cache -t "$NAME" "$REPO"

echo "Restarting container ..."
docker rm -f "$NAME" 2>/dev/null || true
docker run -d --name "$NAME" --restart unless-stopped -p "$PORT:8080" \
  -e WPAI_BASE_URL="${WPAI_BASE_URL:-https://www.keylife.org/wp-load.php}" \
  -e WPAI_IMPORT_KEY="$WPAI_IMPORT_KEY" \
  -e WPAI_IMPORT_ID="${WPAI_IMPORT_ID:-13}" \
  -e ACCESS_TOKEN="${ACCESS_TOKEN:-}" \
  -e SITE_LABEL="${SITE_LABEL:-Key Life Import}" \
  -e POLL_SECONDS="${POLL_SECONDS:-5}" \
  "${EXTRA[@]}" \
  "$NAME"

sleep 2
echo -n "Health: "; curl -fsS "http://localhost:$PORT/healthz" || echo "FAILED"
echo
