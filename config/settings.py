"""All configuration for the Nifty Monthly Iron Fly live bot."""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


@dataclass
class Settings:
    # ── Kite API ──────────────────────────────────────────────────────────────
    KITE_API_KEY: str = os.getenv("KITE_API_KEY", "")
    KITE_BASE_URL: str = "https://api.kite.trade"
    ACCESS_TOKEN_FILE: str = os.getenv(
        "ACCESS_TOKEN_FILE",
        os.path.join(os.path.dirname(__file__), "..", "secrets", "kite_access_token"),
    )

    # ── Strategy (v3 rules — Nifty) ───────────────────────────────────────────
    LOTS: int = int(os.getenv("LOTS", "1"))
    LOT_SIZE: int = 65                     # Nifty NFO lot size (post Nov 20 2024)
    STRIKE_STEP: int = 50                  # Nifty strike interval
    TARGET_PCT: float = 0.50              # 50% of active net credit
    REENTRY_CAP: int = int(os.getenv("REENTRY_CAP", "1"))
    BRIDGE_THRESHOLD: float = 0.01        # 1% max move 9:15→11:00 for re-entry
    ENTRY_HOUR: int = 11
    ENTRY_MINUTE: int = 0
    MARKET_OPEN: str = "09:15"
    MONITOR_END: str = "15:29"
    EXPIRY_CLOSE: str = "15:28"           # settle open legs at this time on expiry day

    # ── Order execution ───────────────────────────────────────────────────────
    ORDER_PRODUCT: str = "NRML"           # NRML for multi-day options
    ORDER_EXCHANGE: str = "NFO"           # NSE F&O
    ORDER_WAIT_SECS: int = 60
    ORDER_TICK_BUFFER: float = 0.50
    MAX_ORDER_ATTEMPTS: int = 3

    # ── Risk ──────────────────────────────────────────────────────────────────
    MAX_DAILY_LOSS_RS: float = float(os.getenv("MAX_DAILY_LOSS_RS", "20000"))
    MIN_MARGIN_BUFFER: float = 1.20       # require 1.2× SPAN before entry
    SL_PCT: float = 0.04                  # 4% of margin blocked as position SL
    MARGIN_PER_LOT: float = 60000.0       # fallback per-lot margin estimate

    # ── Monitoring ────────────────────────────────────────────────────────────
    POLL_INTERVAL_SECS: int = 60

    # ── Notifications ─────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Paths ─────────────────────────────────────────────────────────────────
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    LOG_DIR: str = os.path.join(BASE_DIR, "data", "logs")
    INSTRUMENTS_DIR: str = os.path.join(BASE_DIR, "data", "instruments")
    STATE_FILE: str = os.path.join(BASE_DIR, "data", "state", "trade_state.json")
    TRADE_LOG_CSV: str = os.path.join(BASE_DIR, "data", "state", "trade_log.csv")

    # ── Mode ──────────────────────────────────────────────────────────────────
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    def __post_init__(self):
        import pathlib
        for d in [self.LOG_DIR, self.INSTRUMENTS_DIR,
                  os.path.dirname(self.STATE_FILE)]:
            pathlib.Path(d).mkdir(parents=True, exist_ok=True)


settings = Settings()
