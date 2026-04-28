#!/usr/bin/env bash
# Zerodha daily autologin — runs Go binary with credentials from .env
# Called by cron at 08:45 AM IST (03:15 UTC) Mon–Fri
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [ ! -x bin/zerodha_autologin ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: bin/zerodha_autologin not found. Run: make build" >&2
    exit 1
fi
if [ ! -f .env ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: .env not found. Copy .env.example and fill in credentials." >&2
    exit 1
fi

# Export only required vars from .env — no eval, strip inline comments
while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    k="${line%%=*}"
    v="${line#*=}"
    v="${v%%#*}"
    v="${v%"${v##*[! ]}"}"
    case "$k" in
        KITE_API_KEY|KITE_API_SECRET|KITE_USER_ID|KITE_PASSWORD|KITE_TOTP_SECRET|ACCESS_TOKEN_FILE)
            export "$k"="$v"
            ;;
    esac
done < .env

export ACCESS_TOKEN_FILE="${ACCESS_TOKEN_FILE:-$REPO_ROOT/secrets/kite_access_token}"
mkdir -p "$(dirname "$ACCESS_TOKEN_FILE")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Zerodha autologin..."
./bin/zerodha_autologin
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Autologin complete. Token: $(head -c 6 "$ACCESS_TOKEN_FILE")..."
