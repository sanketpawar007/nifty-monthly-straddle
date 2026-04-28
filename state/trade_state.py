"""JSON-backed persistent state. Survives process restarts."""
import json
from pathlib import Path
from typing import Optional

from strategy.position import CycleState
from utils.logger import get_logger

log = get_logger("trade_state")


class TradeState:
    def __init__(self, state_file: str):
        self.path = Path(state_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
                log.info("State loaded from %s", self.path)
            except Exception as e:
                log.error("Corrupt state file — starting fresh: %s", e)
                self._data = {}
        else:
            self._data = {}

    def save(self):
        self.path.write_text(json.dumps(self._data, indent=2, default=str))

    def get_cycle(self, monthly_expiry: str) -> Optional[CycleState]:
        raw = self._data.get("cycles", {}).get(monthly_expiry)
        return CycleState.from_dict(raw) if raw else None

    def save_cycle(self, cycle: CycleState):
        if "cycles" not in self._data:
            self._data["cycles"] = {}
        self._data["cycles"][cycle.monthly_expiry] = cycle.to_dict()
        self.save()
        log.debug("Cycle saved: %s status=%s", cycle.monthly_expiry, cycle.status)

    def current_expiry(self) -> Optional[str]:
        for expiry in reversed(list(self._data.get("cycles", {}).keys())):
            cycle = self.get_cycle(expiry)
            if cycle and cycle.status != "DONE":
                return expiry
        return None

    def set_flag(self, key: str, value):
        self._data[key] = value
        self.save()

    def get_flag(self, key: str, default=None):
        return self._data.get(key, default)

    def daily_pnl(self) -> float:
        return float(self._data.get("daily_pnl_rs", 0.0))

    def add_pnl(self, amount: float):
        self._data["daily_pnl_rs"] = self.daily_pnl() + amount
        self.save()

    def reset_daily_pnl(self):
        self._data["daily_pnl_rs"] = 0.0
        self.save()
