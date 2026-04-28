# Nifty Monthly Iron Fly — Live Trading Bot

Production-grade monthly Iron Butterfly bot for NSE Nifty 50 options via Zerodha Kite Connect.

## Strategy (v3 Rules)

| Parameter | Value |
|-----------|-------|
| Instrument | Nifty 50 monthly options (NFO) |
| Entry | 1st trading day of month at 11:00 AM IST |
| ATM strike | Nearest 50-pt multiple to Nifty spot (round-half-up) |
| Wings | `round(NC_sell / 50) * 50` points from ATM |
| Target | 50% of NET_NC × lots |
| Re-entry | 1 per cycle (gap gap open before midpoint + bridge OK) |
| Partial exit | Exit one side when spot drifts > NET_NC + 50pt beyond ATM (first half only) |
| Hard exit | 15:28 IST on expiry day |
| Lot size | 65 units (post Nov 20 2024); 75 before |
| Expiry | Last Tuesday of month (post Sep 2025); Last Thursday before |

### SEBI Rule Changes Handled
- **Lot size**: 75 → 65 units (Nov 20, 2024)
- **Expiry day**: Last Thursday → Last Tuesday (Sep 2025 onwards)

## Backtest Results (Oct 2022 – Apr 2026)

See `backtest/nifty_results.csv` — 41 months, 1 lot.

## Project Structure

```
nifty-monthly-ironfly/
├── main.py                     # Bot entry point (systemd service runs this)
├── costs_model.py              # Transaction cost model (matches backtest)
├── config/
│   ├── settings.py             # All parameters (loaded from .env)
│   └── holidays.py             # NSE trading holidays
├── auth/
│   └── token_manager.py        # Reads daily Kite access token
├── market/
│   ├── kite_client.py          # Kite REST API (NFO, Nifty spot)
│   └── instruments.py          # NFO instrument cache + lookup
├── strategy/
│   ├── expiry_calendar.py      # Monthly expiry calendar + cycle builder
│   ├── iron_fly.py             # Entry builder, MTM, target/SL, bridge rule
│   └── position.py             # IronFlyPosition + CycleState dataclasses
├── execution/
│   └── order_manager.py        # Limit → modify → market order flow
├── state/
│   └── trade_state.py          # JSON-backed persistent state
├── risk/
│   └── circuit_breaker.py      # Daily loss limit + margin check
├── notifications/
│   └── telegram.py             # Telegram alerts
├── utils/
│   └── logger.py               # IST-aware rotating file logger
├── cmd/zerodha_autologin/
│   └── main.go                 # Go binary: daily Kite autologin (TOTP)
├── scripts/
│   └── zerodha_autologin.sh    # Shell wrapper for cron
├── deploy/
│   ├── install.sh              # One-shot setup script
│   └── nifty_iron_fly.service  # systemd unit template
├── backtest/
│   ├── nifty_v3_ironfly.py     # Backtesting script
│   └── nifty_results.csv       # Verified results (Oct 2022 – Apr 2026)
└── .github/workflows/
    ├── ci.yml                  # Lint + smoke test on push
    └── deploy.yml              # Manual deploy to VM via SSH
```

## Quick Start (fresh VM)

```bash
# 1. Clone
git clone https://github.com/sanketpawar007/nifty-monthly-straddle
cd nifty-monthly-straddle

# 2. Configure credentials
cp .env.example .env
nano .env   # fill in all values

# 3. Install everything (Go binary + pip + systemd + cron)
bash deploy/install.sh

# 4. Test autologin
make test-token

# 5. Start in DRY_RUN mode first
sudo systemctl start nifty_iron_fly
journalctl -u nifty_iron_fly -f
```

## Configuration (`.env`)

```bash
KITE_API_KEY=...          # Kite Connect app key
KITE_API_SECRET=...       # Kite Connect app secret
KITE_USER_ID=...          # Zerodha client ID (e.g. AB1234)
KITE_PASSWORD=...         # Zerodha login password
KITE_TOTP_SECRET=...      # TOTP base32 secret (from Zerodha 2FA setup)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
LOTS=1
DRY_RUN=true              # MUST be true until dry run validated
MAX_DAILY_LOSS_RS=20000
```

## Service Management

```bash
sudo systemctl status nifty_iron_fly
sudo systemctl restart nifty_iron_fly
journalctl -u nifty_iron_fly -f
journalctl -u nifty_iron_fly --since today
```

## CI/CD

**On push to `main`:** GitHub Actions runs Python syntax check + Go build.

**Manual deploy:** Go to Actions → "Deploy to VM" → Run workflow → type `deploy`.

Required GitHub secrets:
- `VM_HOST` — VM IP address
- `VM_USER` — SSH username (e.g. `ubuntu`)
- `VM_SSH_KEY` — private SSH key content (the `.pem` file contents)

## Running the Backtest

```bash
# Point to your NFO 1-min parquet data
export NIFTY_DATA_DIR=/path/to/nfo/processed/NIFTY
export NIFTY_SPOT_PKL=/path/to/nifty_1min_spot.pkl  # optional spot cache
export LOTS=1

pip3 install pandas pyarrow pytz
python3 backtest/nifty_v3_ironfly.py
```

## Going Live

1. Run full dry-run cycle (observe one complete month)
2. Verify Telegram alerts are received correctly
3. Confirm P&L state matches expectations
4. Set `DRY_RUN=false` in `.env`
5. `sudo systemctl restart nifty_iron_fly`

**Never go live without completing at least one dry-run cycle.**
