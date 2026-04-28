"""
NFO instrument lookup — Nifty monthly options.
Downloads the full NFO instrument list daily, caches it, and provides
lookup for Nifty monthly CE/PE by expiry + strike.

Monthly option pattern: NIFTY\d{2}[A-Z]{3}\d+[CP]E  (e.g. NIFTY26MAY24000CE)
Weekly option pattern:  NIFTY\d{5}\d+[CP]E            (different — skipped here)
"""
import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger("instruments")

_MONTHLY_PATTERN = re.compile(r"^NIFTY\d{2}[A-Z]{3}\d+[CP]E$")


class InstrumentManager:
    def __init__(self, kite_client, cache_dir: str):
        self.kite      = kite_client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._instruments: list = []
        self._monthly_map: dict = {}

    def _cache_path(self) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.cache_dir / f"nfo_instruments_{today}.json"

    def load(self, force_refresh: bool = False):
        cache = self._cache_path()
        if not force_refresh and cache.exists():
            log.info("Loading instruments from cache: %s", cache.name)
            self._instruments = json.loads(cache.read_text())
        else:
            log.info("Downloading NFO instruments from Kite...")
            self._instruments = self.kite.instruments_nfo()
            cache.write_text(json.dumps(self._instruments, default=str))
            log.info("Instruments downloaded: %d rows", len(self._instruments))

        self._build_monthly_map()

    def _build_monthly_map(self):
        self._monthly_map = {}
        for row in self._instruments:
            sym = row.get("tradingsymbol", "")
            if not sym.startswith("NIFTY"):
                continue
            itype = row.get("instrument_type", "")
            if itype not in ("CE", "PE"):
                continue
            if not _MONTHLY_PATTERN.match(sym):
                continue

            try:
                expiry = date.fromisoformat(str(row["expiry"]))
                strike = float(row["strike"])
            except (ValueError, KeyError):
                continue

            key = (expiry, strike, itype)
            self._monthly_map[key] = {
                "symbol":   sym,
                "token":    row.get("instrument_token", ""),
                "lot_size": int(row.get("lot_size", 65)),
                "tick_size": float(row.get("tick_size", 0.05)),
            }

        log.info("Monthly map built: %d Nifty monthly contracts", len(self._monthly_map))

    def get_monthly_expiries(self) -> list:
        return sorted({k[0] for k in self._monthly_map})

    def get_strikes(self, expiry: date, itype: str) -> list:
        return sorted(
            k[1] for k in self._monthly_map if k[0] == expiry and k[2] == itype
        )

    def get_symbol(self, expiry: date, strike: float, itype: str) -> Optional[str]:
        entry = self._monthly_map.get((expiry, strike, itype))
        return entry["symbol"] if entry else None

    def find_nearest_symbol(
        self, expiry: date, target_strike: float, itype: str, max_dist: float = 500
    ) -> Optional[tuple]:
        candidates = [
            (k[1], v["symbol"])
            for k, v in self._monthly_map.items()
            if k[0] == expiry and k[2] == itype
            and abs(k[1] - target_strike) <= max_dist
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda x: abs(x[0] - target_strike))

    def current_monthly_expiry(self) -> Optional[date]:
        today = date.today()
        expiries = self.get_monthly_expiries()
        current_month = [e for e in expiries
                         if e.year == today.year and e.month == today.month and e >= today]
        if current_month:
            return current_month[-1]
        future = [e for e in expiries if e > today]
        return future[0] if future else None

    def get_weekly_expiries(self, after: date, before: date) -> list:
        weekly = set()
        for row in self._instruments:
            sym = row.get("tradingsymbol", "")
            if not sym.startswith("NIFTY"):
                continue
            if row.get("instrument_type") not in ("CE", "PE"):
                continue
            if _MONTHLY_PATTERN.match(sym):
                continue
            try:
                exp = date.fromisoformat(str(row["expiry"]))
            except (ValueError, KeyError):
                continue
            if after < exp < before:
                weekly.add(exp)
        return sorted(weekly)
