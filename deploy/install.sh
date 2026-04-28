#!/usr/bin/env bash
# Full installation script for Nifty Monthly Iron Fly bot.
# Run once from inside the cloned repo: bash deploy/install.sh
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_NAME="nifty_iron_fly"
CRON_TIME="15 3 * * 1-5"   # 08:45 AM IST = 03:15 UTC, Mon–Fri

echo "========================================"
echo "  Nifty Monthly Iron Fly — Installation"
echo "  Install dir: $INSTALL_DIR"
echo "========================================"
echo ""

# ── 1. Go ──────────────────────────────────────────────────────────────────
if ! command -v go &>/dev/null; then
    echo "--- Go not found — installing via apt ---"
    sudo apt-get update -qq
    sudo apt-get install -y golang-go
fi
if ! command -v go &>/dev/null; then
    echo "ERROR: Go installation failed."
    exit 1
fi
echo "Go: $(go version)"

# ── 2. Build autologin binary ──────────────────────────────────────────────
echo ""
echo "--- Building autologin binary ---"
mkdir -p "$INSTALL_DIR/bin"
cd "$INSTALL_DIR"
go build -o bin/zerodha_autologin ./cmd/zerodha_autologin/
chmod +x bin/zerodha_autologin
echo "    OK: $INSTALL_DIR/bin/zerodha_autologin"

# ── 3. Python dependencies ─────────────────────────────────────────────────
echo ""
echo "--- Installing Python dependencies ---"
if [ -n "${VIRTUAL_ENV:-}" ]; then
    pip3 install -r "$INSTALL_DIR/requirements.txt"
else
    pip3 install --user -r "$INSTALL_DIR/requirements.txt"
fi
echo "    OK"

# ── 4. Data directories ────────────────────────────────────────────────────
echo ""
echo "--- Creating directories ---"
mkdir -p "$INSTALL_DIR/data/logs"
mkdir -p "$INSTALL_DIR/data/instruments"
mkdir -p "$INSTALL_DIR/data/state"
mkdir -p "$INSTALL_DIR/secrets"
chmod 700 "$INSTALL_DIR/secrets"
echo "    OK: data/ and secrets/"

# ── 5. .env ────────────────────────────────────────────────────────────────
echo ""
echo "--- Checking .env ---"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    echo ""
    echo "  ⚠️  .env created from template."
    echo "  Fill in ALL values in: $INSTALL_DIR/.env"
    echo "  Then re-run: bash deploy/install.sh"
    echo ""
    exit 0
fi

MISSING=0
for key in KITE_API_KEY KITE_API_SECRET KITE_USER_ID KITE_PASSWORD KITE_TOTP_SECRET; do
    val=$(grep "^${key}=" "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d ' ')
    if [[ -z "$val" || "$val" == *"your_"* || "$val" == *"PLACEHOLDER"* ]]; then
        echo "  ⚠️  $key is not set in .env"
        MISSING=1
    fi
done
if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo "  Fill in the missing values and re-run install."
    exit 1
fi
echo "    OK: all required credentials present"

# ── 6. Systemd service ─────────────────────────────────────────────────────
echo ""
echo "--- Installing systemd service ---"
PYTHON_BIN="$(which python3)"
TEMP_SERVICE="$(mktemp)"

sed \
    -e "s|/path/to/nifty-monthly-ironfly|$INSTALL_DIR|g" \
    -e "s|/usr/bin/python3|$PYTHON_BIN|g" \
    "$INSTALL_DIR/deploy/nifty_iron_fly.service" > "$TEMP_SERVICE"

sudo cp "$TEMP_SERVICE" "/etc/systemd/system/${SERVICE_NAME}.service"
rm "$TEMP_SERVICE"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "    OK: service enabled (not started yet)"

# ── 7. Cron for autologin ──────────────────────────────────────────────────
if ! command -v crontab &>/dev/null; then
    echo "--- cron not found — installing ---"
    sudo apt-get install -y cron
    sudo systemctl enable cron
    sudo systemctl start cron
fi
echo ""
echo "--- Setting up autologin cron (08:45 AM IST, Mon–Fri) ---"
CRON_CMD="${CRON_TIME} cd ${INSTALL_DIR} && ./scripts/zerodha_autologin.sh >> ${INSTALL_DIR}/cron.log 2>&1"

( crontab -l 2>/dev/null | grep -v "zerodha_autologin" || true
  echo "$CRON_CMD"
) | crontab -
echo "    OK: cron job added"
echo "    Verify with: crontab -l"

# ── 8. Done ────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo "  Installation complete"
echo "========================================"
echo ""
echo "Pre-flight checklist:"
echo ""
echo "  1. Run autologin once to get today's token:"
echo "       make test-token"
echo ""
echo "  2. Verify token file exists:"
echo "       cat $INSTALL_DIR/secrets/kite_access_token"
echo ""
echo "  3. Confirm DRY_RUN=true in .env (paper trading first)"
echo ""
echo "  4. Start the bot service:"
echo "       sudo systemctl start $SERVICE_NAME"
echo ""
echo "  5. Watch live logs:"
echo "       journalctl -u $SERVICE_NAME -f"
echo ""
echo "When dry run is validated: set DRY_RUN=false in .env"
echo "  then: sudo systemctl restart $SERVICE_NAME"
echo ""
