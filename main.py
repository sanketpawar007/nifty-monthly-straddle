"""
Nifty Monthly Iron Fly — Live Trading Bot
==========================================
Strategy: v3 rule book (matches nifty_v3_ironfly.py backtest exactly)

Flow each trading day:
  08:45 IST  → Go autologin writes fresh access_token (cron)
  09:00 IST  → Bot loads token, downloads instruments
  09:13 IST  → Arm gap-check (if position open)
  09:15 IST  → Gap check (first candle open vs breakevens)
  09:15-11:00 → Bridge monitoring (if gap triggered, first half only)
  11:00 IST  → Entry (if entry day) OR re-entry (if gap + bridge OK)
  11:00-15:28 → Minute-by-minute target / SL / partial-exit monitoring
  15:28 IST  → Expiry settlement (if expiry day)
  15:35 IST  → EOD summary, state save

SEBI Rules encoded:
  Lot size : 65 units (post Nov 20 2024)
  Expiry   : Last Tuesday of month (post Sep 2025); Last Thursday before that
"""
import sys
import time
import signal
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import settings
from config.holidays import is_trading_day, next_trading_day
from auth.token_manager import TokenManager
from market.kite_client import KiteClient, KiteAuthError
from market.instruments import InstrumentManager
from strategy.expiry_calendar import (
    build_cycle, is_first_half, is_entry_day, is_expiry_day,
    prev_monthly_expiry_for,
)
from strategy.position import CycleState, IronFlyPosition
from strategy.iron_fly import (
    build_entry, compute_mtm, should_exit_target,
    gap_breached, intraday_breached, bridge_period_safe, finalize_pnl,
)
from execution.order_manager import OrderManager
from state.trade_state import TradeState
from risk.circuit_breaker import CircuitBreaker
from notifications.telegram import Telegram
from utils.logger import get_logger
from costs_model import spread_exit_cost_rs

IST = pytz.timezone("Asia/Kolkata")
log = get_logger("main", log_dir=settings.LOG_DIR)

_shutdown = False


def _handle_signal(sig, frame):
    global _shutdown
    log.info("Shutdown signal received (%s) — will exit cleanly.", sig)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def ist_time_str() -> str:
    return now_ist().strftime("%H:%M:%S")


def wait_until(target_hhmm: str, poll_secs: int = 5):
    while not _shutdown:
        if now_ist().strftime("%H:%M") >= target_hhmm:
            return
        time.sleep(poll_secs)


def daily_startup(token_mgr: TokenManager) -> Optional[KiteClient]:
    try:
        token = token_mgr.load()
        kite  = KiteClient(settings.KITE_API_KEY, token, settings.KITE_BASE_URL)
        spot  = kite.nifty_spot()
        log.info("Kite connected. Nifty spot: %.2f", spot)
        return kite
    except FileNotFoundError as e:
        log.error("Token file missing: %s", e)
        return None
    except Exception as e:
        log.error("Startup failed: %s", e)
        return None


def get_or_create_cycle(
    state: TradeState,
    instruments: InstrumentManager,
) -> Optional[CycleState]:
    active_expiry = state.current_expiry()
    if active_expiry:
        return state.get_cycle(active_expiry)

    expiry = instruments.current_monthly_expiry()
    if expiry is None:
        log.error("Cannot determine monthly expiry from instruments")
        return None

    prev_expiry = prev_monthly_expiry_for(expiry)
    cycle_info  = build_cycle(prev_expiry, expiry)
    entry_day   = cycle_info["entry_day"]

    weekly = instruments.get_weekly_expiries(after=entry_day, before=expiry)
    if len(weekly) < 2:
        log.error("Cannot find 2 weekly expiries between %s and %s — cannot set midpoint",
                  entry_day, expiry)
        return None
    midpoint      = weekly[1]
    first_weekly  = weekly[0]
    log.info("Midpoint = 2nd weekly expiry: %s  1st weekly: %s", midpoint, first_weekly)

    cycle = CycleState(
        monthly_expiry      = str(expiry),
        entry_day           = str(entry_day),
        calendar_midpoint   = str(midpoint),
        first_weekly_expiry = str(first_weekly),
        reentry_count       = 0,
        reentry_cap         = settings.REENTRY_CAP,
        bridge_threshold    = settings.BRIDGE_THRESHOLD,
        status              = "WAITING",
    )
    state.save_cycle(cycle)
    log.info("New cycle: expiry=%s entry=%s mid=%s",
             cycle.monthly_expiry, cycle.entry_day, cycle.calendar_midpoint)
    return cycle


def _exit_all_legs(
    pos: IronFlyPosition,
    kite: KiteClient,
    order_mgr: OrderManager,
    tg: Telegram,
    reason: str,
) -> float:
    symbols = [l.symbol for l in pos.active_legs()]
    if not symbols:
        return 0.0
    ltps  = kite.option_ltps(symbols)
    qty   = settings.LOT_SIZE * settings.LOTS
    fills = order_mgr.exit_all_active(pos.active_legs(), ltps, qty, label=reason)
    for leg in pos.active_legs():
        if leg.symbol in fills:
            leg.exit_price  = fills[leg.symbol]
            leg.exited      = True
            leg.exit_reason = reason
    total_exit_cost = 0.0
    for leg in [pos.short_ce, pos.short_pe, pos.long_ce, pos.long_pe]:
        if leg and leg.exited and leg.exit_price > 0:
            side = "buy" if leg.direction == "short" else "sell"
            from costs_model import leg_cost
            total_exit_cost += leg_cost(leg.exit_price, settings.LOTS, side)
    return total_exit_cost


def _exit_spread(short_leg, long_leg, kite: KiteClient, order_mgr: OrderManager, label: str) -> float:
    symbols = [short_leg.symbol, long_leg.symbol]
    ltps    = kite.option_ltps(symbols)
    qty     = settings.LOT_SIZE * settings.LOTS
    fills   = order_mgr.exit_spread(
        short_leg.symbol, ltps.get(short_leg.symbol, short_leg.entry_price),
        long_leg.symbol,  ltps.get(long_leg.symbol,  long_leg.entry_price),
        qty, label=label,
    )
    short_leg.exit_price  = fills.get("short_fill", short_leg.entry_price)
    short_leg.exited      = True
    short_leg.exit_reason = label
    long_leg.exit_price   = fills.get("long_fill", long_leg.entry_price)
    long_leg.exited       = True
    long_leg.exit_reason  = label
    return spread_exit_cost_rs(short_leg.exit_price, long_leg.exit_price, settings.LOTS)


def _enter_opposite_spread(
    short_leg, long_leg,
    kite: KiteClient,
    order_mgr: OrderManager,
    label: str,
) -> tuple:
    """v4: enter a fresh short spread at same strikes, current prices (wings first).
    Returns (short_fill, long_fill, extra_cost_rs) or raises OrderFillError."""
    symbols   = [short_leg.symbol, long_leg.symbol]
    ltps      = kite.option_ltps(symbols)
    short_ltp = ltps.get(short_leg.symbol, short_leg.entry_price)
    long_ltp  = ltps.get(long_leg.symbol,  long_leg.entry_price)
    qty       = settings.LOT_SIZE * settings.LOTS
    log.info("BE-REENTRY %s: sell %s %.2f buy %s %.2f",
             label, short_leg.symbol, short_ltp, long_leg.symbol, long_ltp)
    if settings.DRY_RUN:
        log.info("[DRY RUN] BE-REENTRY %s skipped order placement", label)
        short_fill = short_ltp
        long_fill  = long_ltp
    else:
        long_fill  = order_mgr.execute_leg(long_leg.symbol,  "BUY",  qty, long_ltp)
        short_fill = order_mgr.execute_leg(short_leg.symbol, "SELL", qty, short_ltp)
    from costs_model import leg_cost
    extra_cost = (leg_cost(short_fill, settings.LOTS, "sell") +
                  leg_cost(long_fill,  settings.LOTS, "buy"))
    return short_fill, long_fill, extra_cost


def monitor_loop(
    cycle: CycleState,
    state: TradeState,
    kite: KiteClient,
    order_mgr: OrderManager,
    tg: Telegram,
    cb: CircuitBreaker,
):
    log.info("Entering monitor loop for cycle %s", cycle.monthly_expiry)

    while not _shutdown:
        pos = cycle.active_position()
        if pos is None:
            log.info("No active position — monitor loop exit")
            return

        now         = now_ist()
        t           = now.strftime("%H:%M")
        today       = now.date()
        expiry_date = date.fromisoformat(cycle.monthly_expiry)

        if t < "09:15" or t > "15:30":
            time.sleep(30)
            continue

        if not is_trading_day(today):
            time.sleep(60)
            continue

        if not cb.check_daily_loss(state.daily_pnl()):
            tg.circuit_breaker(f"Daily loss limit ₹{settings.MAX_DAILY_LOSS_RS:,.0f} hit")
            return

        # EXPIRY SETTLEMENT at 15:28
        if today == expiry_date and t >= settings.EXPIRY_CLOSE:
            spot   = kite.nifty_spot()
            for spread_legs in [
                (pos.short_ce, pos.long_ce, "CE"),
                (pos.short_pe, pos.long_pe, "PE"),
            ]:
                s, l, opt_type = spread_legs
                if s and not s.exited:
                    intr_s = max(spot - s.strike, 0) if opt_type == "CE" else max(s.strike - spot, 0)
                    intr_l = max(spot - l.strike, 0) if opt_type == "CE" else max(l.strike - spot, 0)
                    s.exit_price = intr_s; s.exited = True; s.exit_reason = "EXPIRY"
                    l.exit_price = intr_l; l.exited = True; l.exit_reason = "EXPIRY"
            finalize_pnl(pos, 0)
            state.add_pnl(pos.net_pnl_rs)
            cycle.upsert_position(pos)
            cycle.status = "DONE"
            state.save_cycle(cycle)
            tg.expiry_settlement(spot, pos.net_pnl_rs)
            log.info("EXPIRY SETTLEMENT spot=%.0f net_pnl=₹%.0f", spot, pos.net_pnl_rs)
            return

        try:
            all_syms = [l.symbol for l in pos.active_legs()]
            spot     = kite.nifty_spot()
            ltps     = kite.option_ltps(all_syms) if all_syms else {}

            if should_exit_target(pos, ltps):
                exit_cost = _exit_all_legs(pos, kite, order_mgr, tg, "TARGET")
                finalize_pnl(pos, exit_cost)
                state.add_pnl(pos.net_pnl_rs)
                cycle.upsert_position(pos)
                cycle.status = "DONE"
                state.save_cycle(cycle)
                tg.target_exit(pos.net_pnl_rs, compute_mtm(pos, ltps))
                log.info("TARGET EXIT net_pnl=₹%.0f", pos.net_pnl_rs)
                return

            if pos.sl_trigger_rs > 0:
                mtm_rs = compute_mtm(pos, ltps) * settings.LOT_SIZE * settings.LOTS
                if mtm_rs <= -pos.sl_trigger_rs:
                    exit_cost = _exit_all_legs(pos, kite, order_mgr, tg, "SL_MARGIN")
                    finalize_pnl(pos, exit_cost)
                    state.add_pnl(pos.net_pnl_rs)
                    cycle.upsert_position(pos)
                    cycle.status = "DONE"
                    state.save_cycle(cycle)
                    tg.sl_exit("SL_MARGIN", spot, pos.net_pnl_rs)
                    log.info("SL_MARGIN EXIT net_pnl=₹%.0f", pos.net_pnl_rs)
                    return

            in_1h  = is_first_half(today, {
                "calendar_midpoint": date.fromisoformat(cycle.calendar_midpoint)
            })
            breach = intraday_breached(spot, pos)

            if breach:
                if in_1h and t > "09:15":
                    if breach == "UPPER" and not pos.ce_exited:
                        ec = _exit_spread(pos.short_ce, pos.long_ce, kite, order_mgr, "SL_INTRADAY_CE")
                        pos.ce_exited = True
                        pos.exit_cost_rs += ec
                        # Crystallize CE spread P&L (needed for finalize_pnl)
                        pos.crystallized_pnl_rs += (pos.short_ce.pnl_per_unit() + pos.long_ce.pnl_per_unit()) * settings.LOT_SIZE * settings.LOTS
                        # v4: opposite-side re-entry before 1st weekly expiry
                        first_wk = cycle.first_weekly_expiry
                        if (not pos.be_reentry_done and first_wk and
                                str(today) <= first_wk):
                            try:
                                pe_fill, wpe_fill, be_cost = _enter_opposite_spread(
                                    pos.short_pe, pos.long_pe, kite, order_mgr, "BE_REENTRY_PE")
                                from strategy.position import Leg as _Leg
                                qty = settings.LOT_SIZE * settings.LOTS
                                pos.extra_short_pe = _Leg(pos.short_pe.symbol, pos.short_pe.strike, "PE", "short", -qty, pe_fill)
                                pos.extra_long_pe  = _Leg(pos.long_pe.symbol,  pos.long_pe.strike,  "PE", "long",   qty, wpe_fill)
                                pos.entry_cost_rs += be_cost
                                pos.be_reentry_done = True
                                # Recalculate lower BE: combined credit / 2 determines new BE
                                orig_pe_nc = pos.short_pe.entry_price - pos.long_pe.entry_price
                                new_pe_nc  = pe_fill - wpe_fill
                                combined_nc = orig_pe_nc + new_pe_nc
                                pos.lower_be = pos.short_pe.strike - combined_nc / 2.0
                                pos.upper_be = float("inf")  # CE closed, no upper risk
                                tg.error(f"v4 BE-REENTRY PE: added fresh PE spread spot={spot:.0f} lower_be={pos.lower_be:.0f}")
                                log.info("BE-REENTRY PE added: new_lower_be=%.0f", pos.lower_be)
                            except Exception as be_err:
                                log.error("BE re-entry (PE) failed: %s", be_err)
                        cycle.upsert_position(pos)
                        state.save_cycle(cycle)
                        tg.one_sided_exit("UPPER", spot)
                        log.info("ONE-SIDED CE EXIT spot=%.0f be_reentry=%s", spot, pos.be_reentry_done)
                    elif breach == "LOWER" and not pos.pe_exited:
                        ec = _exit_spread(pos.short_pe, pos.long_pe, kite, order_mgr, "SL_INTRADAY_PE")
                        pos.pe_exited = True
                        pos.exit_cost_rs += ec
                        pos.crystallized_pnl_rs += (pos.short_pe.pnl_per_unit() + pos.long_pe.pnl_per_unit()) * settings.LOT_SIZE * settings.LOTS
                        # v4: opposite-side re-entry before 1st weekly expiry
                        first_wk = cycle.first_weekly_expiry
                        if (not pos.be_reentry_done and first_wk and
                                str(today) <= first_wk):
                            try:
                                ce_fill, wce_fill, be_cost = _enter_opposite_spread(
                                    pos.short_ce, pos.long_ce, kite, order_mgr, "BE_REENTRY_CE")
                                from strategy.position import Leg as _Leg
                                qty = settings.LOT_SIZE * settings.LOTS
                                pos.extra_short_ce = _Leg(pos.short_ce.symbol, pos.short_ce.strike, "CE", "short", -qty, ce_fill)
                                pos.extra_long_ce  = _Leg(pos.long_ce.symbol,  pos.long_ce.strike,  "CE", "long",   qty, wce_fill)
                                pos.entry_cost_rs += be_cost
                                pos.be_reentry_done = True
                                orig_ce_nc = pos.short_ce.entry_price - pos.long_ce.entry_price
                                new_ce_nc  = ce_fill - wce_fill
                                combined_nc = orig_ce_nc + new_ce_nc
                                pos.upper_be = pos.short_ce.strike + combined_nc / 2.0
                                pos.lower_be = -float("inf")  # PE closed, no lower risk
                                tg.error(f"v4 BE-REENTRY CE: added fresh CE spread spot={spot:.0f} upper_be={pos.upper_be:.0f}")
                                log.info("BE-REENTRY CE added: new_upper_be=%.0f", pos.upper_be)
                            except Exception as be_err:
                                log.error("BE re-entry (CE) failed: %s", be_err)
                        cycle.upsert_position(pos)
                        state.save_cycle(cycle)
                        tg.one_sided_exit("LOWER", spot)
                        log.info("ONE-SIDED PE EXIT spot=%.0f be_reentry=%s", spot, pos.be_reentry_done)

                    if pos.ce_exited and pos.pe_exited:
                        finalize_pnl(pos, 0)
                        state.add_pnl(pos.net_pnl_rs)
                        cycle.upsert_position(pos)
                        cycle.status = "DONE"
                        state.save_cycle(cycle)
                        tg.sl_exit("BOTH_SIDES", spot, pos.net_pnl_rs)
                        return
                else:
                    exit_cost = _exit_all_legs(pos, kite, order_mgr, tg, "SL_INTRADAY")
                    finalize_pnl(pos, exit_cost)
                    state.add_pnl(pos.net_pnl_rs)
                    cycle.upsert_position(pos)
                    cycle.status = "DONE"
                    state.save_cycle(cycle)
                    tg.sl_exit("SL_INTRADAY", spot, pos.net_pnl_rs)
                    log.info("SL_INTRADAY EXIT spot=%.0f net_pnl=₹%.0f", spot, pos.net_pnl_rs)
                    return

            _ce_p = ltps.get(pos.short_ce.symbol, 0) if pos.short_ce and not pos.ce_exited else 0
            _pe_p = ltps.get(pos.short_pe.symbol, 0) if pos.short_pe and not pos.pe_exited else 0
            log.info("min | spot=%.0f  CE=%.0f PE=%.0f  BE=(%.0f/%.0f)",
                     spot, _ce_p, _pe_p, pos.lower_be, pos.upper_be)

        except KiteAuthError:
            log.error("Auth error during monitoring — skipping this minute")
        except Exception as e:
            log.error("Monitor error: %s", e, exc_info=True)

        time.sleep(settings.POLL_INTERVAL_SECS)


def main():
    log.info("=" * 60)
    log.info("NIFTY IRON FLY BOT — STARTING (dry_run=%s)", settings.DRY_RUN)
    log.info("=" * 60)

    token_mgr = TokenManager(settings.ACCESS_TOKEN_FILE)
    state     = TradeState(settings.STATE_FILE)

    while not _shutdown:
        today = date.today()

        if not is_trading_day(today):
            nxt = next_trading_day(today)
            log.info("Non-trading day. Next: %s. Sleeping.", nxt)
            for _ in range(360):
                if _shutdown: break
                time.sleep(10)
            continue

        t = now_ist().strftime("%H:%M")
        if t < "09:00":
            log.info("Pre-market (%s). Waiting for 09:00.", t)
            wait_until("09:00")

        kite = daily_startup(token_mgr)
        if kite is None:
            tg_bare = Telegram(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, settings.DRY_RUN)
            tg_bare.error("Bot could not connect to Kite. Check autologin.")
            for _ in range(300):
                if _shutdown: break
                time.sleep(10)
            continue

        instruments = InstrumentManager(kite, settings.INSTRUMENTS_DIR)
        instruments.load()

        cycle = get_or_create_cycle(state, instruments)
        if cycle is None:
            log.error("Could not determine cycle — retrying in 5min")
            time.sleep(300)
            continue

        tg        = Telegram(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID, settings.DRY_RUN)
        cb        = CircuitBreaker(kite, settings.MAX_DAILY_LOSS_RS)
        order_mgr = OrderManager(
            kite,
            product     = settings.ORDER_PRODUCT,
            wait_secs   = settings.ORDER_WAIT_SECS,
            tick_buffer = settings.ORDER_TICK_BUFFER,
            dry_run     = settings.DRY_RUN,
        )

        state.reset_daily_pnl()
        expiry_date = date.fromisoformat(cycle.monthly_expiry)
        entry_date  = date.fromisoformat(cycle.entry_day)

        log.info("Cycle: expiry=%s entry=%s mid=%s reentry=%d/%d",
                 cycle.monthly_expiry, cycle.entry_day,
                 cycle.calendar_midpoint, cycle.reentry_count, cycle.reentry_cap)

        if not cycle.active_position():
            tg.startup(cycle.entry_day, cycle.monthly_expiry)

        wait_until("09:13")
        wait_until("09:15")

        active_pos      = cycle.active_position()
        gap_triggered   = False
        gap_open_price  = 0.0

        if active_pos and not active_pos.closed and str(today) > active_pos.entry_day:
            mid_date = date.fromisoformat(cycle.calendar_midpoint)
            if is_first_half(today, {"calendar_midpoint": mid_date}):
                ohlc       = kite.nifty_ohlc()
                spot_open  = ohlc["open"]
                gap        = gap_breached(spot_open, active_pos)
                if gap:
                    exit_cost = _exit_all_legs(active_pos, kite, order_mgr, tg, gap)
                    finalize_pnl(active_pos, exit_cost)
                    state.add_pnl(active_pos.net_pnl_rs)
                    cycle.upsert_position(active_pos)
                    cycle.gap_open_price = spot_open
                    state.save_cycle(cycle)
                    tg.gap_exit(gap, spot_open, active_pos.net_pnl_rs)
                    log.info("GAP EXIT %s spot_open=%.0f", gap, spot_open)
                    gap_triggered  = True
                    gap_open_price = spot_open
                    active_pos     = None

        bridge_max_dev = 0.0
        if gap_triggered and gap_open_price > 0:
            log.info("Bridge window: monitoring Nifty vs gap_open=%.0f until 11:00", gap_open_price)
            while not _shutdown:
                t_b = now_ist().strftime("%H:%M")
                if t_b >= "11:00":
                    break
                try:
                    cur_spot  = kite.nifty_spot()
                    dev = abs(cur_spot - gap_open_price) / gap_open_price
                    bridge_max_dev = max(bridge_max_dev, dev)
                except Exception as e:
                    log.warning("Bridge spot fetch error: %s", e)
                time.sleep(settings.POLL_INTERVAL_SECS)
            log.info("Bridge closed: max_dev=%.3f%% threshold=%.3f%%",
                     bridge_max_dev * 100, cycle.bridge_threshold * 100)

        wait_until("11:00")

        if cycle.status != "DONE" and active_pos is None:
            should_enter = False

            if today == entry_date:
                should_enter = True
                log.info("Entry day — proceeding to entry")
            elif gap_triggered:
                can_reenter = (cycle.reentry_cap == -1 or
                               cycle.reentry_count < cycle.reentry_cap)
                if can_reenter:
                    bridge_ok = bridge_period_safe(
                        kite, gap_open_price,
                        cycle.bridge_threshold,
                        max_deviation=bridge_max_dev,
                    )
                    if bridge_ok:
                        should_enter = True
                        cycle.reentry_count += 1
                        state.save_cycle(cycle)
                        log.info("Re-entry #%d (bridge OK)", cycle.reentry_count)
                    else:
                        tg.reentry(cycle.reentry_count + 1, gap_open_price, 0, bridge_skipped=True)
                else:
                    log.info("Re-entry cap=%d reached — no re-entry", cycle.reentry_cap)
                    tg.error(f"Re-entry cap {cycle.reentry_cap} reached — no further re-entry this cycle")

            if should_enter:
                if not cb.check_margin(estimated_margin=(settings.LOTS * settings.MARGIN_ESTIMATE_PER_LOT)):
                    tg.error("Insufficient margin — skipping entry")
                else:
                    cycle.status = "ENTERING"
                    state.save_cycle(cycle)
                    new_pos = build_entry(
                        kite, instruments, order_mgr, cycle,
                        is_reentry=(today != entry_date),
                        reentry_n=cycle.reentry_count,
                    )
                    if new_pos:
                        cycle.upsert_position(new_pos)
                        cycle.status = "MONITORING"
                        state.save_cycle(cycle)
                        if today != entry_date:
                            tg.reentry(cycle.reentry_count, new_pos.spot_at_entry,
                                       int(new_pos.atm_strike), bridge_skipped=False)
                        tg.entry(
                            new_pos.spot_at_entry, int(new_pos.atm_strike),
                            new_pos.net_credit, new_pos.lower_be, new_pos.upper_be,
                            new_pos.long_pe.strike if new_pos.long_pe else 0,
                            new_pos.long_ce.strike if new_pos.long_ce else 0,
                        )
                    else:
                        cycle.status = "WAITING"
                        state.save_cycle(cycle)
                        tg.error("Entry failed — instrument lookup or order error. Check logs.")

        if cycle.active_position() and not cycle.active_position().closed:
            monitor_loop(cycle, state, kite, order_mgr, tg, cb)

        wait_until("15:35")
        pos_open = (cycle.active_position() is not None and
                    not (cycle.active_position().closed if cycle.active_position() else True))
        tg.daily_summary(str(today), state.daily_pnl(), pos_open)
        log.info("EOD: date=%s net_pnl=₹%.0f", today, state.daily_pnl())

        if today == expiry_date and cycle.status != "DONE":
            cycle.status = "DONE"
            state.save_cycle(cycle)

        now  = now_ist()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        secs = (midnight - now).total_seconds()
        log.info("Sleeping %.0f seconds until %s", secs, midnight.strftime("%Y-%m-%d 00:00"))
        for _ in range(int(secs / 10)):
            if _shutdown: break
            time.sleep(10)

    log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
