"""Daily circuit breaker and pre-entry margin check."""
from config.settings import settings
from utils.logger import get_logger

log = get_logger("circuit_breaker")


class CircuitBreaker:
    def __init__(self, kite_client, max_daily_loss: float):
        self.kite           = kite_client
        self.max_daily_loss = max_daily_loss
        self._triggered     = False

    @property
    def triggered(self) -> bool:
        return self._triggered

    def check_daily_loss(self, daily_pnl_rs: float) -> bool:
        if self._triggered:
            return False
        if daily_pnl_rs <= -abs(self.max_daily_loss):
            log.error("CIRCUIT BREAKER: daily P&L ₹%.0f ≤ -₹%.0f limit",
                      daily_pnl_rs, self.max_daily_loss)
            self._triggered = True
            return False
        return True

    def check_margin(self, estimated_margin: float) -> bool:
        required  = estimated_margin * settings.MIN_MARGIN_BUFFER
        available = self.kite.available_margin()
        if available < required:
            log.error("MARGIN CHECK FAILED: available=₹%.0f < required=₹%.0f",
                      available, required)
            return False
        log.info("Margin OK: available=₹%.0f required=₹%.0f", available, required)
        return True

    def reset(self):
        self._triggered = False
