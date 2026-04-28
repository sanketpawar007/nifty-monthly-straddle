"""
Core Iron Fly strategy engine — v3 rules (Nifty edition).

Strategy rules encoded here (all match the v3_ironfly backtest):
  Entry   : 11:00 AM IST, 1st trading day of month
  ATM     : nearest 50-pt strike to Nifty spot (round-half-up)
  Wings   : round(NC_sell / 50) * 50  (nearest 50-pt, min 50)
  Target  : MTM ≥ 50% × NET_NC × units → full exit
  SL      : MTM ≤ -SL_PCT × NET_NC × units → full exit
  Re-entry: if SL before midpoint, 1 re-entry on same expiry at 11:00 AM
  Partial : if spot drifts ≥ 100% × NET_NC beyond ATM on one side (first half only)
            exit ONLY that 2-leg spread; no SL on remaining leg
  Gap     : 9:15 open outside breakevens → exit immediately
  Bridge  : 9:15→11:00 spot must stay within ±1% of gap_open for re-entry
"""
from datetime import date, datetime
from typing import Optional

import pytz

from config.settings import settings
from market.kite_client import KiteClient
from market.instruments import InstrumentManager
from strategy.expiry_calendar import round_half_up, is_first_half
from strategy.position import IronFlyPosition, Leg, CycleState
from execution.order_manager import OrderManager, OrderFillError
from utils.logger import get_logger

IST = pytz.timezone("Asia/Kolkata")
log = get_logger("iron_fly")


def build_entry(
    kite: KiteClient,
    instruments: InstrumentManager,
    order_mgr: OrderManager,
    cycle: CycleState,
    is_reentry: bool = False,
    reentry_n: int = 0,
) -> Optional[IronFlyPosition]:
    """Build and execute an Iron Fly entry. Returns filled IronFlyPosition or None."""
    expiry  = date.fromisoformat(cycle.monthly_expiry)
    now_ist = datetime.now(tz=IST)

    spot = kite.nifty_spot()
    log.info("Spot at entry: %.2f", spot)

    atm = round_half_up(spot, settings.STRIKE_STEP)
    log.info("ATM strike: %s", atm)

    ce_result = instruments.find_nearest_symbol(expiry, atm, "CE")
    pe_result = instruments.find_nearest_symbol(expiry, atm, "PE")
    if not ce_result or not pe_result:
        log.error("No ATM options found for expiry %s strike %s", expiry, atm)
        return None

    short_ce_k, short_ce_sym = ce_result
    short_pe_k, short_pe_sym = pe_result

    ltps = kite.option_ltps([short_ce_sym, short_pe_sym])
    p_ce = ltps.get(short_ce_sym, 0)
    p_pe = ltps.get(short_pe_sym, 0)
    if p_ce <= 0 or p_pe <= 0:
        log.error("Zero LTP for ATM: CE=%.2f PE=%.2f", p_ce, p_pe)
        return None

    gross_short = p_ce + p_pe
    wing = max(round_half_up(gross_short, settings.STRIKE_STEP), settings.STRIKE_STEP)

    uw_result = instruments.find_nearest_symbol(expiry, short_ce_k + wing, "CE", max_dist=300)
    lw_result = instruments.find_nearest_symbol(expiry, short_pe_k - wing, "PE", max_dist=300)
    if not uw_result or not lw_result:
        log.error("Wing strikes not found. Upper=%s Lower=%s",
                  short_ce_k + wing, short_pe_k - wing)
        return None

    long_ce_k, long_ce_sym = uw_result
    long_pe_k, long_pe_sym = lw_result

    wing_ltps = kite.option_ltps([long_ce_sym, long_pe_sym])
    q_ce = wing_ltps.get(long_ce_sym, 0)
    q_pe = wing_ltps.get(long_pe_sym, 0)
    if q_ce <= 0 or q_pe <= 0:
        log.error("Zero LTP for wings: CE=%.2f PE=%.2f", q_ce, q_pe)
        return None

    net_credit = (p_ce + p_pe) - (q_ce + q_pe)
    if net_credit <= 0:
        log.warning("Net credit ≤ 0 (%.2f) — skipping", net_credit)
        return None

    upper_be = short_ce_k + net_credit
    lower_be = short_pe_k - net_credit
    qty      = settings.LOT_SIZE * settings.LOTS

    log.info("Iron Fly | ATM=%s NC=%.0f wings=(%s/%s) BE=(%.0f/%.0f)",
             atm, net_credit, long_pe_k, long_ce_k, lower_be, upper_be)

    try:
        fills = order_mgr.enter_iron_fly(
            short_ce_sym, p_ce,
            short_pe_sym, p_pe,
            long_ce_sym,  q_ce,
            long_pe_sym,  q_pe,
            qty=qty,
        )
    except OrderFillError as e:
        log.error("Entry order failed: %s", e)
        return None

    p_ce_fill = fills["short_ce_fill"]
    p_pe_fill = fills["short_pe_fill"]
    q_ce_fill = fills["long_ce_fill"]
    q_pe_fill = fills["long_pe_fill"]

    net_credit_actual = (p_ce_fill + p_pe_fill) - (q_ce_fill + q_pe_fill)
    upper_be_actual   = short_ce_k + net_credit_actual
    lower_be_actual   = short_pe_k - net_credit_actual

    pos = IronFlyPosition(
        cycle_expiry    = cycle.monthly_expiry,
        entry_day       = str(date.today()),
        is_reentry      = is_reentry,
        reentry_n       = reentry_n,
        spot_at_entry   = spot,
        atm_strike      = atm,
        wing_dist       = wing,
        net_credit      = net_credit_actual,
        upper_be        = upper_be_actual,
        lower_be        = lower_be_actual,
        entry_timestamp = now_ist.strftime("%Y-%m-%dT%H:%M:%S%z"),
        short_ce = Leg(short_ce_sym, short_ce_k, "CE", "short", -qty, p_ce_fill),
        short_pe = Leg(short_pe_sym, short_pe_k, "PE", "short", -qty, p_pe_fill),
        long_ce  = Leg(long_ce_sym,  long_ce_k,  "CE", "long",   qty, q_ce_fill),
        long_pe  = Leg(long_pe_sym,  long_pe_k,  "PE", "long",   qty, q_pe_fill),
    )

    from costs_model import entry_cost_rs
    pos.entry_cost_rs = entry_cost_rs(p_ce_fill, p_pe_fill, q_ce_fill, q_pe_fill, settings.LOTS)

    try:
        margin_orders = [
            {"exchange": "NFO", "tradingsymbol": short_ce_sym,
             "transaction_type": "SELL", "variety": "regular",
             "product": settings.ORDER_PRODUCT, "order_type": "MARKET", "quantity": qty},
            {"exchange": "NFO", "tradingsymbol": short_pe_sym,
             "transaction_type": "SELL", "variety": "regular",
             "product": settings.ORDER_PRODUCT, "order_type": "MARKET", "quantity": qty},
            {"exchange": "NFO", "tradingsymbol": long_ce_sym,
             "transaction_type": "BUY", "variety": "regular",
             "product": settings.ORDER_PRODUCT, "order_type": "MARKET", "quantity": qty},
            {"exchange": "NFO", "tradingsymbol": long_pe_sym,
             "transaction_type": "BUY", "variety": "regular",
             "product": settings.ORDER_PRODUCT, "order_type": "MARKET", "quantity": qty},
        ]
        margin = kite.basket_margin_rs(margin_orders)
        pos.margin_blocked_rs = margin
        pos.sl_trigger_rs = settings.SL_PCT * margin
        log.info("Margin: ₹%.0f | SL trigger: ₹%.0f", margin, pos.sl_trigger_rs)
    except Exception as e:
        fallback = settings.MARGIN_PER_LOT * settings.LOTS
        pos.margin_blocked_rs = fallback
        pos.sl_trigger_rs = settings.SL_PCT * fallback
        log.warning("basket_margin failed (%s) — fallback ₹%.0f", e, fallback)

    log.info("ENTRY DONE | NC=%.0f BE=(%.0f/%.0f) entry_cost=₹%.0f",
             net_credit_actual, lower_be_actual, upper_be_actual, pos.entry_cost_rs)
    return pos


def compute_mtm(pos: IronFlyPosition, ltps: dict) -> float:
    """Per-unit MTM of all active legs."""
    mtm = 0.0
    for leg in pos.active_legs():
        current = ltps.get(leg.symbol)
        if current is None or current <= 0:
            current = leg.entry_price
        if leg.direction == "short":
            mtm += leg.entry_price - current
        else:
            mtm += current - leg.entry_price
    return mtm


def should_exit_target(pos: IronFlyPosition, ltps: dict) -> bool:
    """True when MTM ≥ 50% of active net credit (v3 §5.1)."""
    mtm       = compute_mtm(pos, ltps)
    threshold = settings.TARGET_PCT * pos.active_net_credit()
    if threshold <= 0:
        return False
    if mtm >= threshold:
        log.info("TARGET: mtm=%.1f >= %.1f (%.0f%% of NC=%.1f)",
                 mtm, threshold, settings.TARGET_PCT * 100, pos.active_net_credit())
        return True
    return False


def gap_breached(spot_open: float, pos: IronFlyPosition) -> Optional[str]:
    """Check 9:15 AM open for gap outside breakevens."""
    if spot_open >= pos.upper_be:
        log.info("GAP_UP: open=%.0f >= upper_be=%.0f", spot_open, pos.upper_be)
        return "GAP_UP"
    if spot_open <= pos.lower_be:
        log.info("GAP_DOWN: open=%.0f <= lower_be=%.0f", spot_open, pos.lower_be)
        return "GAP_DOWN"
    return None


def intraday_breached(spot: float, pos: IronFlyPosition) -> Optional[str]:
    """Check if spot has crossed a breakeven during regular trading."""
    if spot >= pos.upper_be and not pos.ce_exited:
        return "UPPER"
    if spot <= pos.lower_be and not pos.pe_exited:
        return "LOWER"
    return None


def bridge_period_safe(
    kite: KiteClient,
    gap_open: float,
    threshold: float = settings.BRIDGE_THRESHOLD,
    max_deviation: float = None,
) -> bool:
    """
    v3 §6.3: spot must stay within ±threshold% of gap_open during 9:15→11:00.
    max_deviation: pre-computed worst-case deviation across the full window.
    """
    if max_deviation is not None:
        if max_deviation >= threshold:
            log.info("BRIDGE: full-window max_dev=%.2f%% > %.2f%% — skip re-entry",
                     max_deviation * 100, threshold * 100)
            return False
        log.info("Bridge OK: max_dev=%.2f%%", max_deviation * 100)
        return True
    current_spot = kite.nifty_spot()
    move_pct = abs(current_spot - gap_open) / gap_open
    if move_pct >= threshold:
        log.info("BRIDGE: spot=%.0f gap=%.0f move=%.2f%% — skip", current_spot, gap_open, move_pct * 100)
        return False
    log.info("Bridge OK: spot=%.0f move=%.2f%%", current_spot, move_pct * 100)
    return True


def finalize_pnl(pos: IronFlyPosition, exit_cost_rs: float = 0.0):
    """Compute gross and net P&L from all leg fills."""
    gross = 0.0
    for leg in [pos.short_ce, pos.short_pe, pos.long_ce, pos.long_pe]:
        if leg is None:
            continue
        gross += leg.pnl_per_unit() * settings.LOT_SIZE * settings.LOTS

    pos.exit_cost_rs = exit_cost_rs
    pos.gross_pnl_rs = gross
    pos.net_pnl_rs   = gross - pos.entry_cost_rs - exit_cost_rs
    pos.closed       = True
