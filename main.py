"""
Nifty Monthly Iron Fly — Live Trading Bot (v4 rules)
=====================================================
Strategy: Nifty monthly expiry Iron Butterfly

Flow each trading day:
  08:45 IST  → Go autologin writes fresh access_token (cron)
  09:00 IST  → Bot loads token, downloads instruments
  09:15 IST  → Start monitoring if position is already open
  09:30 IST  → Gap info log (informational only, no exit triggered)
  11:00 IST  → Entry (if entry day and no major event)
  11:00-15:20 → Minute monitoring: SL/target (if not voided), Nifty BE breach, MTM
  15:20 IST  → Expiry settlement — square off all residual legs (expiry day only)
  15:35 IST  → EOD summary, state save

Entry Rules:
  Day    : T+1 after monthly expiry (1st trading day of new cycle)
  Time   : 11:00 AM sharp
  ATM    : nearest available strike from options chain to spot
  Wings  : wingspan = ATM CE LTP + ATM PE LTP (exact, no rounding)
  Lots   : 1 lot = 75 qty
  Skip   : if major event within 48h (RBI MPC, Budget, Fed FOMC)

Risk:
  SL     : 4% of margin blocked
  Target : 8% of margin blocked (1:2 RR on capital)

Exit Priority:
  1. Target hit  (8%)  → exit all 4 legs, done
  2. SL hit      (4%)  → exit all 4 legs, done
  3. BE breach         → exit compromised side; re-enter opposite before 1st weekly 3PM
  4. Expiry day 3:20PM → square off all residual

After re-entry: SL/target voided; hold both sides to monthly expiry.
"""
import sys
import time
import signal
import subprocess
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import settings
from config.holidays import is_trading_day, next_trading_day
from config.event_dates import has_major_event_within_48h
from auth.token_manager import TokenManager
from market.kite_client import KiteClient, KiteAuthError
from market.instruments import InstrumentManager
from strategy.expiry_calendar import build_cycle, is_entry_day, is_expiry_day, prev_monthly_expiry_for
from strategy.position import CycleState, IronFlyPosition, Leg
from strategy.iron_fly import (
    build_entry, compute_mtm, should_exit_target,
    intraday_breached, finalize_pnl,
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


def _run_autologin() -> bool:
    """Run the autologin script to get a fresh Kite access token."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "zerodha_autologin.sh")
    if not os.path.isfile(script):
        log.error("Autologin script not found: %s", script)
        return False
    log.info("Running autologin to refresh token...")
    try:
        result = subprocess.run([script], capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log.info("Autologin succeeded: %s", result.stdout.strip())
            return True
        log.error("Autologin failed (rc=%s): %s", result.returncode, result.stderr.strip())
        return False
    except subprocess.TimeoutExpired:
        log.error("Autologin script timed out after 60s")
        return False
    except Exception as e:
        log.error("Autologin error: %s", e)
        return False


def daily_startup(token_mgr: TokenManager) -> Optional[KiteClient]:
    try:
        token = token_mgr.load()
        kite  = KiteClient(settings.KITE_API_KEY, token, settings.KITE_BASE_URL)
        spot  = kite.nifty_spot()
        log.info("Kite connected. Nifty spot: %.2f", spot)
        return kite
    except KiteAuthError:
        log.warning("Token expired (403) — running autologin to refresh...")
        if not _run_autologin():
            log.error("Autologin failed — cannot connect to Kite")
            return None
        try:
            token = token_mgr.refresh()
            kite  = KiteClient(settings.KITE_API_KEY, token, settings.KITE_BASE_URL)
            spot  = kite.nifty_spot()
            log.info("Reconnected after autologin. Nifty spot: %.2f", spot)
            return kite
        except Exception as e:
            log.error("Reconnect after autologin failed: %s", e)
            return None
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
    if len(weekly) < 1:
        log.error("Cannot find weekly expiry between %s and %s", entry_day, expiry)
        return None
    first_weekly = weekly[0]
    midpoint     = weekly[1] if len(weekly) >= 2 else first_weekly
    log.info("1st weekly expiry: %s  midpoint: %s", first_weekly, midpoint)

    cycle = CycleState(
        monthly_expiry      = str(expiry),
        entry_day           = str(entry_day),
        calendar_midpoint   = str(midpoint),
        first_weekly_expiry = str(first_weekly),
        reentry_count       = 0,
        reentry_cap         = 1,
        status              = "WAITING",
    )
    state.save_cycle(cycle)
    log.info("New cycle: expiry=%s entry=%s 1st_weekly=%s",
             cycle.monthly_expiry, cycle.entry_day, cycle.first_weekly_expiry)
    return cycle


def _exit_all_legs(
    pos: IronFlyPosition,
    kite: KiteClient,
    order_mgr: OrderManager,
    tg: Telegram,
    reason: str,
) -> float:
    active = pos.active_legs()
    if not active:
        return 0.0
    ltps  = kite.option_ltps([l.symbol for l in active])
    qty   = settings.LOT_SIZE * settings.LOTS
    fills = order_mgr.exit_all_active(active, ltps, qty, label=reason)
    for leg in active:
        if leg.symbol in fills:
            leg.exit_price  = fills[leg.symbol]
            leg.exited      = True
            leg.exit_reason = reason
    from costs_model import leg_cost
    total_cost = 0.0
    for leg in [pos.short_ce, pos.short_pe, pos.long_ce, pos.long_pe]:
        if leg and leg.exited and leg.exit_price > 0:
            side = "buy" if leg.direction == "short" else "sell"
            total_cost += leg_cost(leg.exit_price, settings.LOTS, side)
    return total_cost


def _exit_side_all_legs(pos, side: str, kite, order_mgr, label: str) -> float:
    """Exit all legs on one side (original + extra). Sets ce_exited/pe_exited."""
    if side == "CE":
        legs = [l for l in [pos.short_ce, pos.long_ce,
                             pos.extra_short_ce, pos.extra_long_ce] if l and not l.exited]
    else:
        legs = [l for l in [pos.short_pe, pos.long_pe,
                             pos.extra_short_pe, pos.extra_long_pe] if l and not l.exited]
    if not legs:
        return 0.0
    ltps  = kite.option_ltps([l.symbol for l in legs])
    qty   = settings.LOT_SIZE * settings.LOTS
    fills = order_mgr.exit_all_active(legs, ltps, qty, label=label)
    for leg in legs:
        if leg.symbol in fills:
            leg.exit_price  = fills[leg.symbol]
            leg.exited      = True
            leg.exit_reason = label
    from costs_model import leg_cost as _lc
    total_cost = 0.0
    for leg in legs:
        if leg.exited and leg.exit_price > 0:
            s = "buy" if leg.direction == "short" else "sell"
            total_cost += _lc(leg.exit_price, settings.LOTS, s)
    pos.crystallized_pnl_rs += sum(l.pnl_per_unit() for l in legs) * settings.LOT_SIZE * settings.LOTS
    pos.exit_cost_rs += total_cost
    if side == "CE":
        pos.ce_exited = True
    else:
        pos.pe_exited = True
    return total_cost


def _enter_opposite_spread(short_leg, long_leg, kite, order_mgr, label: str) -> tuple:
    """Enter a fresh spread at same strikes at current LTPs (wings first).
    Returns (short_fill, long_fill, cost_rs)."""
    ltps      = kite.option_ltps([short_leg.symbol, long_leg.symbol])
    short_ltp = ltps.get(short_leg.symbol, short_leg.entry_price)
    long_ltp  = ltps.get(long_leg.symbol,  long_leg.entry_price)
    qty       = settings.LOT_SIZE * settings.LOTS
    log.info("BE-REENTRY %s: sell %s %.2f  buy %s %.2f",
             label, short_leg.symbol, short_ltp, long_leg.symbol, long_ltp)
    if settings.DRY_RUN:
        short_fill = short_ltp
        long_fill  = long_ltp
    else:
        long_fill  = order_mgr.execute_leg(long_leg.symbol,  "BUY",  qty, long_ltp)
        short_fill = order_mgr.execute_leg(short_leg.symbol, "SELL", qty, short_ltp)
    from costs_model import leg_cost
    cost = leg_cost(short_fill, settings.LOTS, "sell") + leg_cost(long_fill, settings.LOTS, "buy")
    return short_fill, long_fill, cost


def monitor_loop(
    cycle: CycleState,
    state: TradeState,
    kite: KiteClient,
    order_mgr: OrderManager,
    tg: Telegram,
    cb: CircuitBreaker,
    token_mgr: TokenManager,
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

        # ── Expiry settlement: square off all residual legs at 15:20 ────────
        if today == expiry_date and t >= settings.EXPIRY_CLOSE:
            active = pos.active_legs()
            if active:
                log.info("EXPIRY 3:20PM — squaring off %d active legs", len(active))
                exit_cost = _exit_all_legs(pos, kite, order_mgr, tg, "EXPIRY_CLOSE")
            else:
                exit_cost = 0.0
            finalize_pnl(pos, exit_cost)
            state.add_pnl(pos.net_pnl_rs)
            cycle.upsert_position(pos)
            cycle.status = "DONE"
            state.save_cycle(cycle)
            try:
                spot = kite.nifty_spot()
            except Exception:
                spot = 0.0
            tg.expiry_settlement(spot, pos.net_pnl_rs)
            log.info("EXPIRY SETTLEMENT spot=%.0f net_pnl=₹%.0f", spot, pos.net_pnl_rs)
            return

        try:
            all_syms = [l.symbol for l in pos.active_legs()]
            spot     = kite.nifty_spot()
            ltps     = kite.option_ltps(all_syms) if all_syms else {}

            # ── SL and Target (skipped once SL/target voided after BE re-entry) ──
            if not pos.sl_target_voided:
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
                        exit_cost = _exit_all_legs(pos, kite, order_mgr, tg, "SL")
                        finalize_pnl(pos, exit_cost)
                        state.add_pnl(pos.net_pnl_rs)
                        cycle.upsert_position(pos)
                        cycle.status = "DONE"
                        state.save_cycle(cycle)
                        tg.sl_exit("SL", spot, pos.net_pnl_rs)
                        log.info("SL EXIT net_pnl=₹%.0f", pos.net_pnl_rs)
                        return

            # ── BE breach check ──────────────────────────────────────────────
            first_wk    = cycle.first_weekly_expiry
            can_reenter = (
                not pos.be_reentry_done and
                bool(first_wk) and
                (str(today) < first_wk or (str(today) == first_wk and t < "15:00"))
            )
            breach = intraday_breached(spot, pos)

            if breach == "UPPER" and not pos.ce_exited:
                log.info("BE BREACH UPPER: spot=%.0f >= upper_be=%.0f — exiting CE side",
                         spot, pos.upper_be)
                _exit_side_all_legs(pos, "CE", kite, order_mgr, "BE_BREACH_CE")

                if can_reenter:
                    try:
                        pe_fill, wpe_fill, be_cost = _enter_opposite_spread(
                            pos.short_pe, pos.long_pe, kite, order_mgr, "BE_REENTRY_PE")
                        qty = settings.LOT_SIZE * settings.LOTS
                        pos.extra_short_pe = Leg(pos.short_pe.symbol, pos.short_pe.strike,
                                                 "PE", "short", -qty, pe_fill)
                        pos.extra_long_pe  = Leg(pos.long_pe.symbol,  pos.long_pe.strike,
                                                 "PE", "long",   qty, wpe_fill)
                        pos.entry_cost_rs    += be_cost
                        pos.be_reentry_done   = True
                        pos.sl_target_voided  = True
                        log.info("BE-REENTRY PE done — SL/target voided, hold to expiry")
                        tg.error(f"BE-REENTRY PE taken (upper breach) spot={spot:.0f} — SL/target voided")
                    except Exception as be_err:
                        log.error("BE re-entry (PE) failed: %s", be_err)
                else:
                    log.info("No re-entry (outside window or already done) — PE side continues")

                cycle.upsert_position(pos)
                state.save_cycle(cycle)
                tg.one_sided_exit("UPPER", spot)

            elif breach == "LOWER" and not pos.pe_exited:
                log.info("BE BREACH LOWER: spot=%.0f <= lower_be=%.0f — exiting PE side",
                         spot, pos.lower_be)
                _exit_side_all_legs(pos, "PE", kite, order_mgr, "BE_BREACH_PE")

                if can_reenter:
                    try:
                        ce_fill, wce_fill, be_cost = _enter_opposite_spread(
                            pos.short_ce, pos.long_ce, kite, order_mgr, "BE_REENTRY_CE")
                        qty = settings.LOT_SIZE * settings.LOTS
                        pos.extra_short_ce = Leg(pos.short_ce.symbol, pos.short_ce.strike,
                                                 "CE", "short", -qty, ce_fill)
                        pos.extra_long_ce  = Leg(pos.long_ce.symbol,  pos.long_ce.strike,
                                                 "CE", "long",   qty, wce_fill)
                        pos.entry_cost_rs    += be_cost
                        pos.be_reentry_done   = True
                        pos.sl_target_voided  = True
                        log.info("BE-REENTRY CE done — SL/target voided, hold to expiry")
                        tg.error(f"BE-REENTRY CE taken (lower breach) spot={spot:.0f} — SL/target voided")
                    except Exception as be_err:
                        log.error("BE re-entry (CE) failed: %s", be_err)
                else:
                    log.info("No re-entry (outside window or already done) — CE side continues")

                cycle.upsert_position(pos)
                state.save_cycle(cycle)
                tg.one_sided_exit("LOWER", spot)

            # Both sides closed → finalize
            if not pos.active_legs():
                finalize_pnl(pos, 0)
                state.add_pnl(pos.net_pnl_rs)
                cycle.upsert_position(pos)
                cycle.status = "DONE"
                state.save_cycle(cycle)
                tg.sl_exit("BOTH_SIDES_EXITED", spot, pos.net_pnl_rs)
                log.info("ALL SIDES EXITED net_pnl=₹%.0f", pos.net_pnl_rs)
                return

            # ── Per-minute log ───────────────────────────────────────────────
            mtm_rs = compute_mtm(pos, ltps) * settings.LOT_SIZE * settings.LOTS
            _ce_p  = ltps.get(pos.short_ce.symbol, 0) if pos.short_ce and not pos.ce_exited else 0
            _pe_p  = ltps.get(pos.short_pe.symbol, 0) if pos.short_pe and not pos.pe_exited else 0
            log.info("min | spot=%.0f  CE=%.0f PE=%.0f  BE=(%.0f/%.0f) mtm=₹%.0f%s",
                     spot, _ce_p, _pe_p, pos.lower_be, pos.upper_be, mtm_rs,
                     " [SL/TGT VOIDED]" if pos.sl_target_voided else "")

        except KiteAuthError:
            log.warning("Token expired during monitoring — running autologin...")
            if _run_autologin():
                try:
                    new_token = token_mgr.refresh()
                    kite.update_token(new_token)
                    log.info("Token refreshed mid-session — resuming monitoring")
                except Exception as _re:
                    log.error("Token refresh failed after autologin: %s", _re)
            else:
                log.error("Autologin failed mid-session — skipping this minute")
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

        log.info("Cycle: expiry=%s entry=%s 1st_weekly=%s",
                 cycle.monthly_expiry, cycle.entry_day, cycle.first_weekly_expiry)

        active_pos = cycle.active_position()
        if not active_pos:
            tg.startup(cycle.entry_day, cycle.monthly_expiry)

        wait_until("09:15")

        # ── If position is open, start monitoring from 9:15 AM ──────────────
        active_pos = cycle.active_position()
        if active_pos and not active_pos.closed:
            # 9:30 gap informational log
            wait_until("09:30")
            try:
                spot_open = kite.nifty_spot()
                log.info("9:30 gap check: spot=%.0f upper_be=%.0f lower_be=%.0f",
                         spot_open, active_pos.upper_be, active_pos.lower_be)
                if spot_open >= active_pos.upper_be:
                    log.warning("GAP ALERT: spot %.0f >= upper_be %.0f", spot_open, active_pos.upper_be)
                    tg.error(f"GAP alert: Nifty {spot_open:.0f} above upper_be {active_pos.upper_be:.0f}")
                elif spot_open <= active_pos.lower_be:
                    log.warning("GAP ALERT: spot %.0f <= lower_be %.0f", spot_open, active_pos.lower_be)
                    tg.error(f"GAP alert: Nifty {spot_open:.0f} below lower_be {active_pos.lower_be:.0f}")
            except Exception as e:
                log.warning("Gap check error: %s", e)
            monitor_loop(cycle, state, kite, order_mgr, tg, cb, token_mgr)

        # ── Entry at 11:00 AM on entry day (if no position yet) ─────────────
        if cycle.status != "DONE" and not cycle.active_position():
            wait_until("11:00")
            if today == entry_date:
                skip, event_desc = has_major_event_within_48h(today)
                if skip:
                    log.info("ENTRY SKIPPED: major event within 48h — %s", event_desc)
                    tg.error(f"Entry skipped (major event): {event_desc}")
                elif not cb.check_margin(
                    estimated_margin=(settings.LOTS * settings.MARGIN_ESTIMATE_PER_LOT)
                ):
                    tg.error("Insufficient margin — skipping entry")
                else:
                    cycle.status = "ENTERING"
                    state.save_cycle(cycle)
                    new_pos = build_entry(kite, instruments, order_mgr, cycle)
                    if new_pos:
                        cycle.upsert_position(new_pos)
                        cycle.status = "MONITORING"
                        state.save_cycle(cycle)
                        tg.entry(
                            new_pos.spot_at_entry, int(new_pos.atm_strike),
                            new_pos.net_credit, new_pos.lower_be, new_pos.upper_be,
                            new_pos.long_pe.strike if new_pos.long_pe else 0,
                            new_pos.long_ce.strike if new_pos.long_ce else 0,
                        )
                        monitor_loop(cycle, state, kite, order_mgr, tg, cb, token_mgr)
                    else:
                        cycle.status = "WAITING"
                        state.save_cycle(cycle)
                        tg.error("Entry failed — instrument lookup or order error. Check logs.")

        wait_until("15:35")
        pos_open = (cycle.active_position() is not None and
                    not (cycle.active_position().closed if cycle.active_position() else True))
        tg.daily_summary(str(today), state.daily_pnl(), pos_open)
        log.info("EOD: date=%s net_pnl=₹%.0f", today, state.daily_pnl())

        if today == expiry_date and cycle.status != "DONE":
            cycle.status = "DONE"
            state.save_cycle(cycle)

        now      = now_ist()
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        secs     = (midnight - now).total_seconds()
        log.info("Sleeping %.0f seconds until %s", secs, midnight.strftime("%Y-%m-%d 00:00"))
        for _ in range(int(secs / 10)):
            if _shutdown: break
            time.sleep(10)

    log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
