"""Transaction cost model (v3 §8) — matches backtest exactly for consistency."""
from config.settings import settings

BROKERAGE_FLAT = 20.0
BROKERAGE_PCT  = 0.0003
STT_SELL_RATE  = 0.001
EXCHANGE_RATE  = 0.0000325
SEBI_PER_CR    = 10.0
GST_RATE       = 0.18
STAMP_BUY_RATE = 0.00003
SLIPPAGE_PTS   = 1.0     # ₹1/unit; live fills will vary


def leg_cost(premium: float, lots: int, side: str) -> float:
    qty      = settings.LOT_SIZE * lots
    turnover = premium * qty
    brokerage = min(BROKERAGE_FLAT, BROKERAGE_PCT * turnover)
    stt      = STT_SELL_RATE * turnover if side == "sell" else 0.0
    exchange = EXCHANGE_RATE * turnover
    sebi     = SEBI_PER_CR * turnover / 1e7
    gst      = GST_RATE * (brokerage + exchange)
    stamp    = STAMP_BUY_RATE * turnover if side == "buy" else 0.0
    slippage = SLIPPAGE_PTS * qty
    return brokerage + stt + exchange + sebi + gst + stamp + slippage


def entry_cost_rs(p_ce: float, p_pe: float, q_ce: float, q_pe: float, lots: int) -> float:
    return (
        leg_cost(p_ce, lots, "sell") +
        leg_cost(p_pe, lots, "sell") +
        leg_cost(q_ce, lots, "buy")  +
        leg_cost(q_pe, lots, "buy")
    )


def spread_exit_cost_rs(short_px: float, long_px: float, lots: int) -> float:
    return leg_cost(short_px, lots, "buy") + leg_cost(long_px, lots, "sell")
