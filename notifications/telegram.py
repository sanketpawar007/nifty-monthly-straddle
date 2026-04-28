"""Telegram alert client — no async dependency."""
import requests
from utils.logger import get_logger

log = get_logger("telegram")
_API = "https://api.telegram.org/bot{token}/sendMessage"


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = True):
        self.token   = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.enabled = bool(token and chat_id)

    def _send(self, text: str) -> bool:
        if not self.enabled:
            log.info("[TG disabled] %s", text[:80])
            return False
        try:
            resp = requests.post(
                _API.format(token=self.token),
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            log.warning("Telegram send failed: %s", e)
            return False

    def _tag(self) -> str:
        return " [DRY RUN]" if self.dry_run else ""

    def startup(self, entry_day: str, expiry: str):
        return self._send(
            f"🚀 <b>Nifty Iron Fly Bot started{self._tag()}</b>\n"
            f"Entry day: {entry_day}  |  Expiry: {expiry}"
        )

    def entry(self, spot: float, atm: int, net_credit: float,
              lower_be: float, upper_be: float,
              long_pe_k: float, long_ce_k: float):
        return self._send(
            f"📌 <b>ENTRY{self._tag()}</b>\n"
            f"Nifty Spot: {spot:,.0f}  ATM: {atm:,}\n"
            f"Net Credit: {net_credit:.0f} pts\n"
            f"Wings: {long_pe_k:,} PE — {long_ce_k:,} CE\n"
            f"Breakevens: {lower_be:,.0f} — {upper_be:,.0f}"
        )

    def target_exit(self, net_pnl: float, mtm: float):
        return self._send(
            f"✅ <b>TARGET HIT{self._tag()}</b>\n"
            f"MTM: {mtm:.0f} pts  |  Net P&L: ₹{net_pnl:,.0f}"
        )

    def gap_exit(self, direction: str, spot_open: float, net_pnl: float):
        emoji = "📈" if direction == "GAP_UP" else "📉"
        return self._send(
            f"{emoji} <b>GAP SL — {direction}{self._tag()}</b>\n"
            f"Open: {spot_open:,.0f}  |  Net P&L: ₹{net_pnl:,.0f}"
        )

    def reentry(self, n: int, spot: float, atm: int, bridge_skipped: bool = False):
        if bridge_skipped:
            return self._send(
                f"⚠️ <b>BRIDGE RULE — Re-entry skipped{self._tag()}</b>\n"
                f"Spot still moving ({spot:,.0f}) — waiting for tomorrow"
            )
        return self._send(
            f"🔁 <b>RE-ENTRY #{n}{self._tag()}</b>\n"
            f"Spot: {spot:,.0f}  New ATM: {atm:,}"
        )

    def one_sided_exit(self, side: str, spot: float):
        emoji = "📈" if side == "UPPER" else "📉"
        return self._send(
            f"{emoji} <b>ONE-SIDED EXIT — {side} BE breached{self._tag()}</b>\n"
            f"Spot: {spot:,.0f}  |  {side.replace('UPPER','CE').replace('LOWER','PE')} spread exited"
        )

    def sl_exit(self, reason: str, spot: float, net_pnl: float):
        return self._send(
            f"🛑 <b>SL EXIT — {reason}{self._tag()}</b>\n"
            f"Spot: {spot:,.0f}  |  Net P&L: ₹{net_pnl:,.0f}"
        )

    def expiry_settlement(self, spot: float, net_pnl: float):
        return self._send(
            f"🏁 <b>EXPIRY SETTLEMENT{self._tag()}</b>\n"
            f"Expiry spot: {spot:,.0f}  |  Net P&L: ₹{net_pnl:,.0f}"
        )

    def error(self, msg: str):
        return self._send(f"🚨 <b>ERROR{self._tag()}</b>\n{msg}")

    def daily_summary(self, date_str: str, net_pnl: float, position_open: bool):
        status = "📊 Position open" if position_open else "💤 No position"
        return self._send(
            f"📋 <b>EOD Summary — {date_str}{self._tag()}</b>\n"
            f"Today P&L: ₹{net_pnl:,.0f}  |  {status}"
        )

    def circuit_breaker(self, reason: str):
        return self._send(f"🚨 <b>CIRCUIT BREAKER{self._tag()}</b>\n{reason}\nBot halted for today.")
