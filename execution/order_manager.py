"""
Order manager: place, wait, modify, market-convert for a single leg.
v3 Section 11.1: BUY wings first, then SELL shorts.
"""
import time
from typing import Optional

from market.kite_client import KiteClient, KiteAPIError
from utils.logger import get_logger

log = get_logger("order_manager")


class OrderFillError(Exception):
    pass


class OrderManager:
    def __init__(
        self,
        kite: KiteClient,
        product: str = "NRML",
        wait_secs: int = 60,
        tick_buffer: float = 0.50,
        dry_run: bool = True,
        tag: str = "ironfly",
    ):
        self.kite        = kite
        self.product     = product
        self.wait_secs   = wait_secs
        self.tick_buffer = tick_buffer
        self.dry_run     = dry_run
        self.tag         = tag

    def _limit_price(self, ltp: float, txn: str) -> float:
        if txn == "BUY":
            return round(ltp + self.tick_buffer, 2)
        return round(max(ltp - self.tick_buffer, 0.05), 2)

    def execute_leg(self, symbol: str, txn: str, qty: int, ltp: float) -> float:
        """
        Place LIMIT order → wait → modify once → market.
        Returns fill price (or LTP in dry-run).
        """
        price = self._limit_price(ltp, txn)

        if self.dry_run:
            log.info("[DRY RUN] %s %s qty=%d price=%.2f", txn, symbol, qty, price)
            return ltp

        try:
            order_id = self.kite.place_order(
                tradingsymbol=symbol,
                transaction_type=txn,
                quantity=qty,
                price=price,
                product=self.product,
                order_type="LIMIT",
                tag=self.tag,
            )
        except KiteAPIError as e:
            raise OrderFillError(f"Place order failed for {symbol}: {e}")

        time.sleep(min(self.wait_secs, 30))
        status = self.kite.get_order_status(order_id)
        if status.get("status") == "COMPLETE":
            fill = float(status.get("average_price", price))
            log.info("[ORDER] FILLED %s @ %.2f", symbol, fill)
            return fill

        new_price = round(price * 1.005 if txn == "BUY" else price * 0.995, 2)
        try:
            self.kite.modify_order(order_id, new_price)
        except KiteAPIError as e:
            log.warning("Modify failed, will try market: %s", e)

        time.sleep(30)
        status = self.kite.get_order_status(order_id)
        if status.get("status") == "COMPLETE":
            fill = float(status.get("average_price", new_price))
            log.info("[ORDER] FILLED (after modify) %s @ %.2f", symbol, fill)
            return fill

        log.warning("Limit not filled — converting to MARKET: %s", symbol)
        self.kite.cancel_order(order_id)
        try:
            mkt_id = self.kite.place_order(
                tradingsymbol=symbol,
                transaction_type=txn,
                quantity=qty,
                price=0,
                product=self.product,
                order_type="MARKET",
                tag=self.tag,
            )
            time.sleep(5)
            mkt_status = self.kite.get_order_status(mkt_id)
            fill = float(mkt_status.get("average_price", ltp))
            log.info("[ORDER] MARKET FILLED %s @ %.2f", symbol, fill)
            return fill
        except KiteAPIError as e:
            raise OrderFillError(f"Market order failed for {symbol}: {e}")

    def enter_iron_fly(
        self,
        short_ce_sym: str, short_ce_ltp: float,
        short_pe_sym: str, short_pe_ltp: float,
        long_ce_sym:  str, long_ce_ltp: float,
        long_pe_sym:  str, long_pe_ltp: float,
        qty: int,
    ) -> dict:
        """v3 §11.1: BUY wings first, then SELL shorts."""
        fills = {}
        log.info("Entering Iron Fly (qty=%d, dry_run=%s)", qty, self.dry_run)
        fills["long_ce_fill"]  = self.execute_leg(long_ce_sym,  "BUY",  qty, long_ce_ltp)
        fills["long_pe_fill"]  = self.execute_leg(long_pe_sym,  "BUY",  qty, long_pe_ltp)
        fills["short_ce_fill"] = self.execute_leg(short_ce_sym, "SELL", qty, short_ce_ltp)
        fills["short_pe_fill"] = self.execute_leg(short_pe_sym, "SELL", qty, short_pe_ltp)
        return fills

    def exit_spread(
        self,
        short_sym: str, short_ltp: float,
        long_sym:  str, long_ltp: float,
        qty: int,
        label: str = "",
    ) -> dict:
        log.info("Exiting spread %s qty=%d", label, qty)
        return {
            "short_fill": self.execute_leg(short_sym, "BUY",  qty, short_ltp),
            "long_fill":  self.execute_leg(long_sym,  "SELL", qty, long_ltp),
        }

    def exit_all_active(self, legs: list, ltps: dict, qty: int, label: str = "") -> dict:
        fills = {}
        log.info("Exiting all active legs [%s] qty=%d", label, qty)
        for leg in legs:
            if leg.exited:
                continue
            ltp  = ltps.get(leg.symbol, leg.entry_price)
            txn  = "BUY" if leg.direction == "short" else "SELL"
            fills[leg.symbol] = self.execute_leg(leg.symbol, txn, qty, ltp)
        return fills
