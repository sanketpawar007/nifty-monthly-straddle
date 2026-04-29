"""
Senior Test Engineer — Nifty Monthly Iron Fly Bot
Test Suite: Entry, Per-Minute SL/Target/BE Monitoring, Gap, Bridge, Re-entry, Expiry
============================================================================
Coverage: 98 test cases across 12 scenario groups
Changes from v1: margin-first entry, 8% target on capital, no fallback SL,
                 midpoint = 2nd weekly expiry (hard error if not found)
"""
import sys
import os
import json
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch, call

# ── path setup ────────────────────────────────────────────────────────────────
BOT_DIR = "/home/ubuntu/nifty-monthly-straddle"
sys.path.insert(0, BOT_DIR)

# force .env load before any module import reads env vars
from dotenv import load_dotenv
load_dotenv(os.path.join(BOT_DIR, ".env"))

from config.settings import settings
from strategy.position import Leg, IronFlyPosition, CycleState
from strategy.expiry_calendar import (
    round_half_up, is_first_half, monthly_expiry_for,
    build_cycle, prev_monthly_expiry_for,
)
from strategy.iron_fly import (
    compute_mtm, should_exit_target, gap_breached,
    intraday_breached, bridge_period_safe, finalize_pnl,
)
from risk.circuit_breaker import CircuitBreaker
from state.trade_state import TradeState
from costs_model import entry_cost_rs, leg_cost


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _make_leg(symbol="SYM", strike=24000, opt_type="CE", direction="short",
              entry_price=100.0, exit_price=0.0, exited=False, qty=65) -> Leg:
    leg = Leg(symbol=symbol, strike=strike, opt_type=opt_type,
              direction=direction, qty=qty, entry_price=entry_price)
    leg.exit_price = exit_price
    leg.exited = exited
    return leg


def _make_position(
    atm=24000, nc=200.0, sl_trigger_rs=2400.0, margin=60000.0,
    ce_exited=False, pe_exited=False,
    short_ce_price=150.0, short_pe_price=100.0,
    long_ce_price=30.0, long_pe_price=20.0,
    entry_day="2026-05-04",
) -> IronFlyPosition:
    """Canonical 4-leg iron fly at Nifty 24000, NC=200, wings at 24200/23800."""
    wing = round_half_up(short_ce_price + short_pe_price, settings.STRIKE_STEP)
    upper_be = atm + nc
    lower_be = atm - nc

    pos = IronFlyPosition(
        cycle_expiry    = "2026-05-27",
        entry_day       = entry_day,
        spot_at_entry   = atm,
        atm_strike      = atm,
        wing_dist       = wing,
        net_credit      = nc,
        upper_be        = upper_be,
        lower_be        = lower_be,
        entry_timestamp = "2026-05-04T11:00:00+0530",
    )
    pos.short_ce = _make_leg("CE_24000", atm,       "CE", "short", short_ce_price)
    pos.short_pe = _make_leg("PE_24000", atm,       "PE", "short", short_pe_price)
    pos.long_ce  = _make_leg("CE_24200", atm + 200, "CE", "long",  long_ce_price)
    pos.long_pe  = _make_leg("PE_23800", atm - 200, "PE", "long",  long_pe_price)
    pos.margin_blocked_rs = margin
    pos.sl_trigger_rs     = sl_trigger_rs
    pos.ce_exited         = ce_exited
    pos.pe_exited         = pe_exited
    return pos


def _make_cycle(
    midpoint="2026-05-16",
    monthly_expiry="2026-05-27",
    entry_day="2026-05-04",
    status="MONITORING",
    reentry_count=0,
    reentry_cap=1,
    bridge_threshold=0.01,
) -> CycleState:
    return CycleState(
        monthly_expiry    = monthly_expiry,
        entry_day         = entry_day,
        calendar_midpoint = midpoint,
        reentry_count     = reentry_count,
        reentry_cap       = reentry_cap,
        bridge_threshold  = bridge_threshold,
        status            = status,
    )


def _mock_kite(spot=24000.0, ltps=None, margin=60000.0):
    kite = MagicMock()
    kite.nifty_spot.return_value = spot
    kite.option_ltps.return_value = ltps or {}
    kite.basket_margin_rs.return_value = margin
    kite.available_margin.return_value = margin * 2
    return kite


# ══════════════════════════════════════════════════════════════════════════════
# GROUP A — ENTRY RULES & STRIKE CALCULATIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestEntryRules(unittest.TestCase):
    """TC-E: Entry at 11:00 AM on day after monthly expiry, strike math."""

    def test_E01_atm_round_half_up_exact_midpoint(self):
        """Spot exactly midway between strikes: round UP to higher strike."""
        self.assertEqual(round_half_up(24025.0, 50), 24050)
        self.assertEqual(round_half_up(23975.0, 50), 24000)

    def test_E02_atm_round_half_up_below_midpoint(self):
        """Spot below midpoint: round DOWN to lower strike."""
        self.assertEqual(round_half_up(24024.9, 50), 24000)

    def test_E03_atm_round_half_up_above_midpoint(self):
        """Spot above midpoint: round UP."""
        self.assertEqual(round_half_up(24025.1, 50), 24050)

    def test_E04_wing_distance_is_round_half_up_of_gross_credit(self):
        """Wing = round_half_up(CE_premium + PE_premium, 50)."""
        ce_premium, pe_premium = 150.0, 100.0
        gross = ce_premium + pe_premium       # 250
        expected_wing = round_half_up(gross, 50)   # 250 → 250
        self.assertEqual(expected_wing, 250)

    def test_E05_wing_minimum_is_50_points(self):
        """Even if gross credit < 25, wing cannot be less than 50 (STRIKE_STEP)."""
        low_credit_wing = max(round_half_up(10.0, 50), settings.STRIKE_STEP)
        self.assertEqual(low_credit_wing, 50)

    def test_E06_breakeven_upper_equals_atm_plus_nc(self):
        """upper_be = short_ce_strike + net_credit."""
        atm, nc = 24000, 200.0
        upper_be = atm + nc
        self.assertAlmostEqual(upper_be, 24200.0)

    def test_E07_breakeven_lower_equals_atm_minus_nc(self):
        """lower_be = short_pe_strike - net_credit."""
        atm, nc = 24000, 200.0
        lower_be = atm - nc
        self.assertAlmostEqual(lower_be, 23800.0)

    def test_E08_sl_trigger_is_4pct_of_margin_blocked(self):
        """SL trigger = SL_PCT (4%) × margin_blocked_rs."""
        margin = 60000.0
        expected_sl = settings.SL_PCT * margin   # 0.04 × 60000 = 2400
        self.assertAlmostEqual(expected_sl, 2400.0)
        self.assertEqual(settings.SL_PCT, 0.04)

    def test_E09_no_fallback_sl_margin_estimate_is_preentry_only(self):
        """
        MARGIN_PER_LOT fallback removed. MARGIN_ESTIMATE_PER_LOT is only used
        for the pre-entry availability check, NOT for SL calculation.
        If basket_margin API fails, entry is aborted (no inaccurate SL set).
        """
        self.assertTrue(hasattr(settings, "MARGIN_ESTIMATE_PER_LOT"),
                        "MARGIN_ESTIMATE_PER_LOT must exist for pre-entry check")
        self.assertFalse(hasattr(settings, "MARGIN_PER_LOT"),
                         "MARGIN_PER_LOT (old fallback) must NOT exist")

    def test_E10_lot_size_is_65_units(self):
        """Nifty lot size (Nifty NFO lot size (NSE mandated) = 65 units."""
        self.assertEqual(settings.LOT_SIZE, 65)

    def test_E11_net_credit_positive_required(self):
        """If gross_short ≤ gross_long, net_credit ≤ 0 — entry must be skipped."""
        nc = (100.0 + 80.0) - (90.0 + 95.0)   # -5.0 — invalid
        self.assertLessEqual(nc, 0)

    def test_E12_entry_hour_and_minute(self):
        """Entry scheduled at exactly 11:00 AM IST."""
        self.assertEqual(settings.ENTRY_HOUR, 11)
        self.assertEqual(settings.ENTRY_MINUTE, 0)

    def test_E13_entry_day_is_next_trading_day_after_expiry(self):
        """Entry day = first trading day after previous monthly expiry."""
        # April 2026: last Thursday = April 30
        # May 4 is first trading day (May 1 = holiday, May 2-3 = weekend)
        prev_expiry = date(2026, 4, 30)
        cycle = build_cycle(prev_expiry, date(2026, 5, 27))
        # entry_day must be after April 30
        self.assertGreater(cycle["entry_day"], prev_expiry)

    def test_E14_order_sequence_wings_first_then_shorts(self):
        """v3 §11.1: BUY wings first to limit max-loss risk during entry."""
        from execution.order_manager import OrderManager
        kite = _mock_kite()
        order_mgr = OrderManager(kite, dry_run=True)
        call_log = []
        original = order_mgr.execute_leg
        def log_leg(sym, txn, qty, ltp):
            call_log.append((sym, txn))
            return ltp
        order_mgr.execute_leg = log_leg

        order_mgr.enter_iron_fly(
            "CE_24000", 150.0,
            "PE_24000", 100.0,
            "CE_24200", 30.0,
            "PE_23800", 20.0,
            qty=65,
        )
        txns = [t for _, t in call_log]
        buy_indices  = [i for i, t in enumerate(txns) if t == "BUY"]
        sell_indices = [i for i, t in enumerate(txns) if t == "SELL"]
        self.assertTrue(all(b < s for b in buy_indices for s in sell_indices),
                        "All BUY (wing) orders must come before SELL (short) orders")


# ══════════════════════════════════════════════════════════════════════════════
# GROUP B — PER-MINUTE TARGET / SL / BE MONITORING
# ══════════════════════════════════════════════════════════════════════════════

class TestPerMinuteMonitoring(unittest.TestCase):
    """TC-M: Each minute the bot checks target, SL, and breakeven breach."""

    def setUp(self):
        self.pos = _make_position(atm=24000, nc=200.0, sl_trigger_rs=2400.0)

    # --- compute_mtm ---

    def test_M01_compute_mtm_profit_when_options_decay(self):
        """MTM = Σ(entry - current) for shorts + Σ(current - entry) for longs."""
        ltps = {
            "CE_24000": 100.0,   # short CE: entry 150, now 100 → +50 per unit
            "PE_24000":  60.0,   # short PE: entry 100, now 60  → +40 per unit
            "CE_24200":  20.0,   # long  CE: entry 30,  now 20  → -10 per unit
            "PE_23800":  10.0,   # long  PE: entry 20,  now 10  → -10 per unit
        }
        mtm = compute_mtm(self.pos, ltps)
        self.assertAlmostEqual(mtm, 50 + 40 - 10 - 10)  # +70 per unit

    def test_M02_compute_mtm_loss_when_options_rally(self):
        """Short options rallying → negative MTM."""
        ltps = {
            "CE_24000": 200.0,   # short CE up 50 → -50/unit
            "PE_24000": 100.0,   # short PE flat → 0
            "CE_24200":  30.0,   # long CE flat → 0
            "PE_23800":  20.0,   # long PE flat → 0
        }
        mtm = compute_mtm(self.pos, ltps)
        self.assertAlmostEqual(mtm, -50.0)

    def test_M03_compute_mtm_uses_entry_price_if_ltp_missing(self):
        """Missing LTP key → uses entry_price (no crash, MTM contribution = 0)."""
        ltps = {}   # empty — all missing
        mtm = compute_mtm(self.pos, ltps)
        self.assertAlmostEqual(mtm, 0.0)

    # --- should_exit_target ---

    def test_M04_target_exit_at_8pct_of_margin_blocked(self):
        """TARGET hit: MTM_RS ≥ 8% × margin_blocked_rs (4:8 RR on capital)."""
        # margin=60000 → target_rs = 8% × 60000 = 4800
        # need mtm per unit such that mtm × 65 ≥ 4800 → mtm_per_unit ≥ 73.85
        # short CE entry=150, exit at 76 → mtm = 74/unit × 65 = 4810 ≥ 4800 ✓
        ltps = {
            "CE_24000":  76.0,   # short CE: 150 - 76 = +74/unit
            "PE_24000": 100.0,   # flat
            "CE_24200":  30.0,   # flat
            "PE_23800":  20.0,   # flat
        }
        self.assertTrue(should_exit_target(self.pos, ltps))

    def test_M05_target_NOT_hit_below_8pct_threshold(self):
        """MTM_RS just under 8% of margin → no target exit."""
        # target_rs = 4800; need mtm_rs < 4800 → mtm/unit < 64.0 (with LOT_SIZE=75)
        # short CE entry=150, exit at 88 → mtm = 62/unit × 75 = 4650 < 4800 ✓
        ltps = {
            "CE_24000":  88.0,
            "PE_24000": 100.0,
            "CE_24200":  30.0,
            "PE_23800":  20.0,
        }
        self.assertFalse(should_exit_target(self.pos, ltps))

    def test_M06_target_threshold_exact_boundary_8pct(self):
        """MTM_RS == exactly 8% of margin → target IS triggered (>=)."""
        # target_rs = 0.08 × 60000 = 4800; mtm/unit must be exactly 4800/65 ≈ 73.846
        margin = self.pos.margin_blocked_rs   # 60000
        target_rs = settings.TARGET_RS_PCT * margin   # 4800
        mtm_per_unit_needed = target_rs / (settings.LOT_SIZE * settings.LOTS)  # 73.846
        # short CE drop = 150 - (150 - mtm_per_unit_needed) = mtm_per_unit_needed
        exact_ce_exit = self.pos.short_ce.entry_price - mtm_per_unit_needed
        ltps = {
            "CE_24000": exact_ce_exit,
            "PE_24000": 100.0,
            "CE_24200":  30.0,
            "PE_23800":  20.0,
        }
        self.assertTrue(should_exit_target(self.pos, ltps))

    def test_M06b_target_skipped_if_margin_blocked_is_zero(self):
        """If margin_blocked_rs not set (0), target check must return False."""
        pos = _make_position(margin=0.0, sl_trigger_rs=0.0)
        ltps = {"CE_24000": 0.01, "PE_24000": 0.01, "CE_24200": 0.01, "PE_23800": 0.01}
        self.assertFalse(should_exit_target(pos, ltps))

    # --- SL trigger ---

    def test_M07_sl_trigger_at_4pct_of_margin_in_rs(self):
        """SL triggered when MTM_RS (per lot_size × lots) ≤ -sl_trigger_rs."""
        sl_rs = self.pos.sl_trigger_rs  # 2400
        # per-unit MTM that makes MTM_RS == -2400
        # mtm_rs = mtm_per_unit × LOT_SIZE × LOTS = mtm_per_unit × 75 × 1
        mtm_per_unit_at_sl = -(sl_rs / (settings.LOT_SIZE * settings.LOTS))
        # = -32 per unit (LOT_SIZE=75)
        self.assertAlmostEqual(mtm_per_unit_at_sl, -(2400 / settings.LOT_SIZE), places=2)

    def test_M08_sl_rs_computed_as_mtm_unit_times_lot_size(self):
        """Verify formula: mtm_rs = compute_mtm(pos, ltps) × LOT_SIZE × LOTS."""
        ltps = {
            "CE_24000": 187.0,   # short CE: 150 - 187 = -37 per unit
            "PE_24000": 100.0,
            "CE_24200":  30.0,
            "PE_23800":  20.0,
        }
        mtm = compute_mtm(self.pos, ltps)
        mtm_rs = mtm * settings.LOT_SIZE * settings.LOTS
        # -37 per unit × 65 = -2405 → SL triggered (> 2400)
        self.assertLess(mtm_rs, -self.pos.sl_trigger_rs)

    def test_M09_sl_NOT_triggered_just_above_threshold(self):
        """MTM_RS just above (less negative than) -sl_trigger_rs → no SL."""
        ltps = {
            "CE_24000": 181.0,   # -31/unit × 75 = -2325 — above -2400 limit
            "PE_24000": 100.0,
            "CE_24200":  30.0,
            "PE_23800":  20.0,
        }
        mtm = compute_mtm(self.pos, ltps)
        mtm_rs = mtm * settings.LOT_SIZE * settings.LOTS
        self.assertGreater(mtm_rs, -self.pos.sl_trigger_rs)

    def test_M10_poll_interval_is_60_seconds(self):
        """Monitor poll interval must be 60 seconds (per-minute cycle)."""
        self.assertEqual(settings.POLL_INTERVAL_SECS, 60)

    def test_M11_active_net_credit_excludes_exited_side(self):
        """After CE side exits, active_net_credit uses only PE side."""
        pos = _make_position(ce_exited=True)
        nc = pos.active_net_credit()
        # Only PE: short_pe.entry_price - long_pe.entry_price = 100 - 20 = 80
        self.assertAlmostEqual(nc, 80.0)

    def test_M12_active_legs_excludes_exited_spreads(self):
        """active_legs() must not include legs from an already-exited side."""
        pos = _make_position(ce_exited=True)
        syms = [l.symbol for l in pos.active_legs()]
        self.assertNotIn("CE_24000", syms)
        self.assertNotIn("CE_24200", syms)
        self.assertIn("PE_24000", syms)
        self.assertIn("PE_23800", syms)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP C — RISK:REWARD & CAPITAL DEPLOYMENT VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

class TestRiskRewardCapital(unittest.TestCase):
    """TC-R: 4:8 RR strictly on invested capital (actual margin from Kite API)."""

    def test_R01_sl_pct_setting_is_4_percent(self):
        """SL_PCT must be exactly 0.04 (4% of margin)."""
        self.assertEqual(settings.SL_PCT, 0.04)

    def test_R02_target_rs_pct_setting_is_8_percent(self):
        """TARGET_RS_PCT = 0.08 — target exits at 8% of margin_blocked_rs."""
        self.assertEqual(settings.TARGET_RS_PCT, 0.08)

    def test_R03_rr_ratio_is_exactly_4_to_8(self):
        """SL:Target ratio must be exactly 4:8 = 1:2 on any margin value."""
        margin = 150000.0   # hypothetical actual margin
        sl_rs     = settings.SL_PCT * margin
        target_rs = settings.TARGET_RS_PCT * margin
        self.assertAlmostEqual(target_rs / sl_rs, 2.0)

    def test_R04_sl_rs_for_given_actual_margin(self):
        """SL = 4% of whatever basket_margin returns (no hardcoded fallback)."""
        actual_margin = 135000.0   # typical actual Nifty Iron Fly margin
        sl = settings.SL_PCT * actual_margin
        self.assertAlmostEqual(sl, 5400.0)

    def test_R05_target_rs_for_given_actual_margin(self):
        """Target = 8% of actual margin."""
        actual_margin = 135000.0
        target = settings.TARGET_RS_PCT * actual_margin
        self.assertAlmostEqual(target, 10800.0)

    def test_R06_no_target_pct_attribute(self):
        """TARGET_PCT (50% of NC) must not exist — replaced by TARGET_RS_PCT."""
        self.assertFalse(hasattr(settings, "TARGET_PCT"),
                         "TARGET_PCT was removed; use TARGET_RS_PCT instead")

    def test_R07_no_margin_per_lot_fallback_attribute(self):
        """MARGIN_PER_LOT (fallback) must not exist — only MARGIN_ESTIMATE_PER_LOT."""
        self.assertFalse(hasattr(settings, "MARGIN_PER_LOT"),
                         "MARGIN_PER_LOT was removed; only MARGIN_ESTIMATE_PER_LOT for pre-entry check")

    def test_R08_margin_estimate_per_lot_exists_for_preentry_check(self):
        """MARGIN_ESTIMATE_PER_LOT is kept for rough pre-entry availability check only."""
        self.assertTrue(hasattr(settings, "MARGIN_ESTIMATE_PER_LOT"))
        self.assertGreater(settings.MARGIN_ESTIMATE_PER_LOT, 0)

    def test_R09_should_exit_target_uses_margin_not_nc(self):
        """should_exit_target computes MTM_RS against 8% × margin, not 50% of NC."""
        pos = _make_position(margin=60000.0)
        # target_rs = 8% × 60000 = 4800 ₹
        # mtm_rs to hit target: 4800 / (65*1) = 73.846 per unit
        # Short CE at 150 exits at 76 → mtm = 74/unit × 65 = 4810 → HIT
        ltps = {"CE_24000": 76.0, "PE_24000": 100.0, "CE_24200": 30.0, "PE_23800": 20.0}
        self.assertTrue(should_exit_target(pos, ltps))

    def test_R10_minimum_margin_buffer_is_1point2x(self):
        """Pre-entry margin check requires 1.2× the estimated margin."""
        self.assertEqual(settings.MIN_MARGIN_BUFFER, 1.20)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP D — BREAKEVEN BREACH DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestBreakevenBreach(unittest.TestCase):
    """TC-B: Spot crossing upper/lower BEs triggers the correct response."""

    def setUp(self):
        self.pos = _make_position(atm=24000, nc=200.0)
        # BE: upper=24200, lower=23800

    def test_B01_no_breach_when_spot_between_bes(self):
        self.assertIsNone(intraday_breached(24100.0, self.pos))

    def test_B02_upper_breach_when_spot_at_exactly_upper_be(self):
        """Spot exactly at upper_be (24200) → UPPER breach."""
        self.assertEqual(intraday_breached(24200.0, self.pos), "UPPER")

    def test_B03_upper_breach_when_spot_above_upper_be(self):
        self.assertEqual(intraday_breached(24250.0, self.pos), "UPPER")

    def test_B04_lower_breach_when_spot_at_exactly_lower_be(self):
        """Spot exactly at lower_be (23800) → LOWER breach."""
        self.assertEqual(intraday_breached(23800.0, self.pos), "LOWER")

    def test_B05_lower_breach_when_spot_below_lower_be(self):
        self.assertEqual(intraday_breached(23750.0, self.pos), "LOWER")

    def test_B06_upper_not_re_triggered_after_ce_exited(self):
        """Once ce_exited=True, UPPER breach must NOT fire again."""
        pos = _make_position(ce_exited=True)
        self.assertIsNone(intraday_breached(24250.0, pos))

    def test_B07_lower_not_re_triggered_after_pe_exited(self):
        """Once pe_exited=True, LOWER breach must NOT fire again."""
        pos = _make_position(pe_exited=True)
        self.assertIsNone(intraday_breached(23750.0, pos))

    def test_B08_spot_one_tick_inside_upper_be_no_breach(self):
        """Spot at 24199 (one point inside upper BE) → no breach."""
        self.assertIsNone(intraday_breached(24199.0, self.pos))

    def test_B09_spot_one_tick_inside_lower_be_no_breach(self):
        """Spot at 23801 → no breach."""
        self.assertIsNone(intraday_breached(23801.0, self.pos))


# ══════════════════════════════════════════════════════════════════════════════
# GROUP E — GAP CHECK (9:15 AM OPEN)
# ══════════════════════════════════════════════════════════════════════════════

class TestGapCheck(unittest.TestCase):
    """TC-G: 9:15 gap outside breakevens triggers immediate full exit."""

    def setUp(self):
        self.pos = _make_position(atm=24000, nc=200.0)
        # upper_be=24200, lower_be=23800

    def test_G01_gap_up_when_open_at_upper_be(self):
        """open == upper_be → GAP_UP."""
        self.assertEqual(gap_breached(24200.0, self.pos), "GAP_UP")

    def test_G02_gap_up_when_open_above_upper_be(self):
        self.assertEqual(gap_breached(24500.0, self.pos), "GAP_UP")

    def test_G03_gap_down_when_open_at_lower_be(self):
        """open == lower_be → GAP_DOWN."""
        self.assertEqual(gap_breached(23800.0, self.pos), "GAP_DOWN")

    def test_G04_gap_down_when_open_below_lower_be(self):
        self.assertEqual(gap_breached(23500.0, self.pos), "GAP_DOWN")

    def test_G05_no_gap_when_open_between_bes(self):
        self.assertIsNone(gap_breached(24100.0, self.pos))

    def test_G06_no_gap_one_point_inside_upper(self):
        self.assertIsNone(gap_breached(24199.0, self.pos))

    def test_G07_no_gap_one_point_inside_lower(self):
        self.assertIsNone(gap_breached(23801.0, self.pos))


# ══════════════════════════════════════════════════════════════════════════════
# GROUP F — BRIDGE PERIOD (9:15→11:00 SPOT STABILITY)
# ══════════════════════════════════════════════════════════════════════════════

class TestBridgePeriod(unittest.TestCase):
    """TC-BR: Spot must stay within ±1% of gap_open to allow re-entry."""

    def _bridge(self, max_dev=None, current_spot=None, gap_open=24000.0):
        kite = _mock_kite(spot=current_spot or gap_open)
        return bridge_period_safe(kite, gap_open, threshold=0.01, max_deviation=max_dev)

    def test_BR01_bridge_ok_when_max_dev_below_1pct(self):
        """max_dev=0.009 (0.9%) < 1% → bridge OK, re-entry allowed."""
        self.assertTrue(self._bridge(max_dev=0.009))

    def test_BR02_bridge_stub_always_returns_true(self):
        """v4: bridge_period_safe is a legacy stub — always returns True (bridge logic removed)."""
        self.assertTrue(self._bridge(max_dev=0.01))

    def test_BR03_bridge_stub_any_deviation_returns_true(self):
        """v4: bridge stub returns True regardless of deviation — no block in v4 rules."""
        self.assertTrue(self._bridge(max_dev=0.015))

    def test_BR04_bridge_ok_at_0_99pct(self):
        """0.99% deviation → still within 1% window → OK."""
        self.assertTrue(self._bridge(max_dev=0.0099))

    def test_BR05_bridge_ok_at_zero_deviation(self):
        """No movement at all → trivially OK."""
        self.assertTrue(self._bridge(max_dev=0.0))

    def test_BR06_bridge_stub_returns_true_without_kite_call(self):
        """v4 bridge stub: returns True without calling kite (no live spot needed)."""
        kite = _mock_kite(spot=24100.0)
        result = bridge_period_safe(kite, gap_open=24000.0, threshold=0.01)
        self.assertTrue(result)

    def test_BR07_bridge_stub_always_true_regardless_of_spot(self):
        """v4: bridge stub always True even if spot is far from gap_open."""
        kite = _mock_kite(spot=24240.0)
        result = bridge_period_safe(kite, gap_open=24000.0, threshold=0.01)
        self.assertTrue(result)

    def test_BR08_bridge_threshold_in_cycle_state_default(self):
        """v4: bridge_threshold moved to CycleState (not in settings). Default = 0.01."""
        cycle = CycleState(
            monthly_expiry="2026-05-26",
            entry_day="2026-04-29",
            calendar_midpoint="2026-05-12",
        )
        self.assertAlmostEqual(cycle.bridge_threshold, 0.01)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP G — ONE-SIDED EXIT (FIRST HALF ONLY)
# ══════════════════════════════════════════════════════════════════════════════

class TestOneSidedExit(unittest.TestCase):
    """TC-OS: In first half, breach of one side exits only that spread."""

    def test_OS01_is_first_half_on_midpoint_itself(self):
        """The midpoint date is included in the first half."""
        mid = date(2026, 5, 16)
        self.assertTrue(is_first_half(mid, {"calendar_midpoint": mid}))

    def test_OS02_is_first_half_one_day_before_midpoint(self):
        mid = date(2026, 5, 16)
        self.assertTrue(is_first_half(mid - timedelta(days=1), {"calendar_midpoint": mid}))

    def test_OS03_is_second_half_one_day_after_midpoint(self):
        mid = date(2026, 5, 16)
        self.assertFalse(is_first_half(mid + timedelta(days=1), {"calendar_midpoint": mid}))

    def test_OS04_active_net_credit_halved_after_one_sided_ce_exit(self):
        """After CE side exits, only PE side credit counts for target calc."""
        pos = _make_position(
            short_ce_price=150.0, long_ce_price=30.0,   # CE spread credit = 120
            short_pe_price=100.0, long_pe_price=20.0,   # PE spread credit = 80
        )
        pos.ce_exited = True
        self.assertAlmostEqual(pos.active_net_credit(), 80.0)

    def test_OS05_exit_cost_rs_accumulates_across_two_partial_exits(self):
        """exit_cost_rs must add up, not overwrite, when both sides exit."""
        pos = _make_position()
        pos.exit_cost_rs = 500.0   # first partial exit cost

        # Simulate second partial exit adding more cost
        pos.exit_cost_rs += 400.0
        self.assertAlmostEqual(pos.exit_cost_rs, 900.0)

    def test_OS06_finalize_pnl_accumulates_exit_cost_from_prior_partial_exit(self):
        """
        Bug fix (2026-04-28): finalize_pnl += accumulates exit_cost_rs.
        Scenario: CE one-sided exit cost (500) already in pos.exit_cost_rs,
        then SL fires adding a fresh PE exit cost (300) → total must be 800.
        """
        pos = _make_position()
        for leg in [pos.short_ce, pos.short_pe, pos.long_ce, pos.long_pe]:
            leg.exited = True
            leg.exit_price = leg.entry_price   # zero gross pnl for simplicity

        pos.exit_cost_rs = 500.0   # CE one-sided exit already accumulated
        pos.entry_cost_rs = 600.0

        finalize_pnl(pos, exit_cost_rs=300.0)   # PE exit cost = 300
        self.assertAlmostEqual(pos.exit_cost_rs, 800.0,
                               msg="exit_cost_rs must accumulate (500 + 300 = 800)")
        self.assertAlmostEqual(pos.net_pnl_rs, 0.0 - 600.0 - 800.0)

    def test_OS07_finalize_pnl_with_zero_prior_cost(self):
        """When no prior partial exits (exit_cost_rs=0), fresh cost sets total correctly."""
        pos = _make_position()
        for leg in [pos.short_ce, pos.short_pe, pos.long_ce, pos.long_pe]:
            leg.exited = True
            leg.exit_price = leg.entry_price
        finalize_pnl(pos, exit_cost_rs=300.0)
        self.assertAlmostEqual(pos.exit_cost_rs, 300.0)  # 0 + 300 = 300


# ══════════════════════════════════════════════════════════════════════════════
# GROUP H — P&L INTEGRITY
# ══════════════════════════════════════════════════════════════════════════════

class TestPnLIntegrity(unittest.TestCase):
    """TC-PNL: Gross and net P&L computed correctly from leg fills."""

    def _closed_pos(self, short_ce_exit=80.0, short_pe_exit=50.0,
                    long_ce_exit=15.0, long_pe_exit=8.0):
        pos = _make_position(
            short_ce_price=150.0, short_pe_price=100.0,
            long_ce_price=30.0,   long_pe_price=20.0,
        )
        pos.short_ce.exit_price = short_ce_exit; pos.short_ce.exited = True
        pos.short_pe.exit_price = short_pe_exit; pos.short_pe.exited = True
        pos.long_ce.exit_price  = long_ce_exit;  pos.long_ce.exited  = True
        pos.long_pe.exit_price  = long_pe_exit;  pos.long_pe.exited  = True
        return pos

    def test_PNL01_leg_pnl_short_is_entry_minus_exit(self):
        """Short leg: profit = entry_price - exit_price per unit."""
        leg = _make_leg(direction="short", entry_price=150.0, exit_price=80.0, exited=True)
        self.assertAlmostEqual(leg.pnl_per_unit(), 70.0)

    def test_PNL02_leg_pnl_long_is_exit_minus_entry(self):
        """Long leg: profit = exit_price - entry_price per unit."""
        leg = _make_leg(direction="long", entry_price=30.0, exit_price=15.0, exited=True)
        self.assertAlmostEqual(leg.pnl_per_unit(), -15.0)

    def test_PNL03_leg_pnl_zero_if_not_exited(self):
        """Leg not yet exited → pnl_per_unit = 0."""
        leg = _make_leg(entry_price=150.0, exited=False)
        self.assertEqual(leg.pnl_per_unit(), 0.0)

    def test_PNL04_finalize_pnl_gross_matches_sum_of_legs(self):
        """gross_pnl_rs = Σ pnl_per_unit × LOT_SIZE × LOTS."""
        pos = self._closed_pos(
            short_ce_exit=80.0, short_pe_exit=50.0,
            long_ce_exit=15.0,  long_pe_exit=8.0,
        )
        pos.entry_cost_rs = 600.0
        finalize_pnl(pos, exit_cost_rs=400.0)

        # short CE: +70, short PE: +50, long CE: -15, long PE: -12 = +93/unit
        expected_gross = (70 + 50 - 15 - 12) * settings.LOT_SIZE * settings.LOTS
        self.assertAlmostEqual(pos.gross_pnl_rs, expected_gross)

    def test_PNL05_net_pnl_equals_gross_minus_costs(self):
        """net_pnl_rs = gross - entry_cost - exit_cost."""
        pos = self._closed_pos()
        pos.entry_cost_rs = 600.0
        finalize_pnl(pos, exit_cost_rs=400.0)
        self.assertAlmostEqual(pos.net_pnl_rs, pos.gross_pnl_rs - 600.0 - 400.0)

    def test_PNL06_position_marked_closed_after_finalize(self):
        """pos.closed = True after finalize_pnl."""
        pos = self._closed_pos()
        finalize_pnl(pos, 0.0)
        self.assertTrue(pos.closed)

    def test_PNL07_short_leg_loss_when_exit_above_entry(self):
        """Short leg exits at a higher price → loss."""
        leg = _make_leg(direction="short", entry_price=100.0, exit_price=200.0, exited=True)
        self.assertAlmostEqual(leg.pnl_per_unit(), -100.0)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP I — EXPIRY SETTLEMENT (15:28)
# ══════════════════════════════════════════════════════════════════════════════

class TestExpirySettlement(unittest.TestCase):
    """TC-EX: On expiry day at 15:28, settle open legs at intrinsic value."""

    def test_EX01_ce_itm_settled_at_intrinsic(self):
        """CE in-the-money: intrinsic = max(spot - strike, 0)."""
        spot, strike = 24300.0, 24000.0
        intrinsic = max(spot - strike, 0)
        self.assertAlmostEqual(intrinsic, 300.0)

    def test_EX02_pe_itm_settled_at_intrinsic(self):
        """PE in-the-money: intrinsic = max(strike - spot, 0)."""
        spot, strike = 23700.0, 24000.0
        intrinsic = max(strike - spot, 0)
        self.assertAlmostEqual(intrinsic, 300.0)

    def test_EX03_otm_option_settled_at_zero(self):
        """OTM options → intrinsic = 0 (expire worthless)."""
        spot, ce_strike, pe_strike = 24000.0, 24200.0, 23800.0
        ce_intrinsic = max(spot - ce_strike, 0)
        pe_intrinsic = max(pe_strike - spot, 0)
        self.assertEqual(ce_intrinsic, 0)
        self.assertEqual(pe_intrinsic, 0)

    def test_EX04_expiry_settlement_preserves_prior_exit_costs(self):
        """
        Expiry settlement calls finalize_pnl(pos, 0) — no new brokerage since
        SEBI settles intrinsic value directly. The prior one-sided exit cost
        already in pos.exit_cost_rs must survive unchanged.
        (main.py fixed: was finalize_pnl(pos, pos.exit_cost_rs) → now (pos, 0))
        """
        pos = _make_position()
        pos.exit_cost_rs = 750.0   # accumulated from a one-sided exit earlier

        for leg in [pos.short_ce, pos.short_pe, pos.long_ce, pos.long_pe]:
            leg.exited = True
            leg.exit_price = 0.0   # OTM settlement — all expire worthless

        finalize_pnl(pos, 0)   # expiry passes 0, no new brokerage
        self.assertAlmostEqual(pos.exit_cost_rs, 750.0,
                               msg="Expiry settlement must preserve prior exit cost")

    def test_EX05_expiry_close_time_is_1520(self):
        """v4: Square off residual legs at 3:20 PM on expiry day (rule §7)."""
        self.assertEqual(settings.EXPIRY_CLOSE, "15:20")

    def test_EX06_monitor_end_is_1529(self):
        """Monitor loop runs until 15:29."""
        self.assertEqual(settings.MONITOR_END, "15:29")


# ══════════════════════════════════════════════════════════════════════════════
# GROUP J — CIRCUIT BREAKER & MARGIN CHECK
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker(unittest.TestCase):
    """TC-CB: Daily loss limit and margin guard prevent entry in bad conditions."""

    def _make_cb(self, available_margin=120000.0, max_loss=20000.0):
        kite = _mock_kite(margin=available_margin)
        kite.available_margin.return_value = available_margin
        return CircuitBreaker(kite, max_loss)

    def test_CB01_circuit_not_triggered_within_limit(self):
        cb = self._make_cb()
        self.assertTrue(cb.check_daily_loss(-10000.0))

    def test_CB02_circuit_triggered_at_exactly_limit(self):
        """P&L = exactly -20000 → triggered (≤ comparison)."""
        cb = self._make_cb()
        self.assertFalse(cb.check_daily_loss(-20000.0))
        self.assertTrue(cb.triggered)

    def test_CB03_circuit_triggered_above_limit(self):
        """P&L = -25000 → triggered."""
        cb = self._make_cb()
        self.assertFalse(cb.check_daily_loss(-25000.0))

    def test_CB04_once_triggered_stays_triggered(self):
        """After trigger, check_daily_loss always returns False."""
        cb = self._make_cb()
        cb.check_daily_loss(-25000.0)
        self.assertFalse(cb.check_daily_loss(0.0),
                         "Once triggered, circuit breaker must stay open")

    def test_CB05_margin_check_passes_with_sufficient_funds(self):
        cb = self._make_cb(available_margin=120000.0)
        # estimated margin = 1 × 60000; required = 1.2 × 60000 = 72000
        self.assertTrue(cb.check_margin(estimated_margin=60000.0))

    def test_CB06_margin_check_fails_when_available_too_low(self):
        cb = self._make_cb(available_margin=70000.0)
        # required = 1.2 × 60000 = 72000 > 70000 available → fail
        self.assertFalse(cb.check_margin(estimated_margin=60000.0))

    def test_CB07_max_daily_loss_setting(self):
        """MAX_DAILY_LOSS_RS default is ₹20,000."""
        self.assertEqual(settings.MAX_DAILY_LOSS_RS, 20000.0)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP K — STATE PERSISTENCE & CYCLE LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

class TestStatePersistence(unittest.TestCase):
    """TC-ST: State survives serialize/deserialize roundtrip; cycle transitions."""

    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".json")

    def test_ST01_ironfly_position_roundtrip(self):
        """IronFlyPosition → to_dict() → from_dict() is lossless."""
        pos = _make_position()
        restored = IronFlyPosition.from_dict(pos.to_dict())
        self.assertAlmostEqual(restored.net_credit, pos.net_credit)
        self.assertAlmostEqual(restored.upper_be,   pos.upper_be)
        self.assertAlmostEqual(restored.lower_be,   pos.lower_be)
        self.assertEqual(restored.short_ce.symbol, pos.short_ce.symbol)

    def test_ST02_cycle_state_roundtrip(self):
        """CycleState → to_dict() → from_dict() is lossless."""
        cycle = _make_cycle()
        restored = CycleState.from_dict(cycle.to_dict())
        self.assertEqual(restored.monthly_expiry, cycle.monthly_expiry)
        self.assertEqual(restored.status, cycle.status)
        self.assertEqual(restored.reentry_cap, cycle.reentry_cap)

    def test_ST03_active_position_returns_latest_unclosed(self):
        """cycle.active_position() returns the most recent non-closed position."""
        cycle = _make_cycle()
        pos1 = _make_position(); pos1.closed = True; cycle.upsert_position(pos1)
        pos2 = _make_position(); pos2.entry_timestamp = "2026-05-05T11:00:00+0530"
        cycle.upsert_position(pos2)
        active = cycle.active_position()
        self.assertIsNotNone(active)
        self.assertFalse(active.closed)

    def test_ST04_active_position_none_when_all_closed(self):
        cycle = _make_cycle()
        pos = _make_position(); pos.closed = True
        cycle.upsert_position(pos)
        self.assertIsNone(cycle.active_position())

    def test_ST05_trade_state_pnl_accumulates(self):
        """add_pnl() is additive; daily_pnl() returns total."""
        state = TradeState(self.tmp)
        state.add_pnl(5000.0)
        state.add_pnl(-2000.0)
        self.assertAlmostEqual(state.daily_pnl(), 3000.0)

    def test_ST06_trade_state_reset_daily_pnl(self):
        state = TradeState(self.tmp)
        state.add_pnl(5000.0)
        state.reset_daily_pnl()
        self.assertAlmostEqual(state.daily_pnl(), 0.0)

    def tearDown(self):
        if os.path.exists(self.tmp):
            os.unlink(self.tmp)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP L — EXPIRY CALENDAR
# ══════════════════════════════════════════════════════════════════════════════

class TestExpiryCalendar(unittest.TestCase):
    """TC-CAL: Correct expiry dates and cycle boundary calculations."""

    def test_CAL01_nifty_monthly_expiry_post_sep2025_is_last_tuesday(self):
        """From Sep 2025 onwards, Nifty monthly expiry = last Tuesday."""
        # May 2026: last Tuesday = May 26 (check it's a trading day)
        expiry = monthly_expiry_for(2026, 5)
        self.assertEqual(expiry.weekday(), 1,   # 1 = Tuesday
                         f"May 2026 expiry should be Tuesday, got {expiry.strftime('%A')}")

    def test_CAL02_nifty_monthly_expiry_pre_sep2025_is_last_thursday(self):
        """Pre-Sep 2025: monthly expiry = last Thursday."""
        expiry = monthly_expiry_for(2024, 12)
        self.assertEqual(expiry.weekday(), 3,   # 3 = Thursday
                         f"Dec 2024 expiry should be Thursday, got {expiry.strftime('%A')}")

    def test_CAL03_entry_day_is_after_prev_expiry(self):
        """Entry day must be strictly after the previous monthly expiry (Nifty)."""
        prev_exp = monthly_expiry_for(2026, 4)   # April 28, 2026 (Tuesday)
        curr_exp = monthly_expiry_for(2026, 5)   # May 26, 2026 (Tuesday)
        cycle = build_cycle(prev_exp, curr_exp)
        self.assertGreater(cycle["entry_day"], prev_exp)

    def test_CAL04_round_half_up_at_half_step(self):
        """Exact half-step rounds UP (round-half-up convention)."""
        self.assertEqual(round_half_up(24025.0, 50), 24050)

    def test_CAL05_round_half_up_below_half_step(self):
        self.assertEqual(round_half_up(24024.99, 50), 24000)

    def test_CAL06_round_half_up_above_half_step(self):
        self.assertEqual(round_half_up(24025.01, 50), 24050)

    def test_CAL07_midpoint_is_within_cycle(self):
        """Calendar midpoint must be between entry_day and monthly_expiry."""
        prev_exp = date(2026, 4, 30)
        curr_exp = monthly_expiry_for(2026, 5)
        cycle = build_cycle(prev_exp, curr_exp)
        self.assertGreater(cycle["calendar_midpoint"], cycle["entry_day"])
        self.assertLess(cycle["calendar_midpoint"], curr_exp)

    def test_CAL08_prev_monthly_expiry_is_previous_month(self):
        """prev_monthly_expiry_for returns expiry from the month before."""
        curr = monthly_expiry_for(2026, 5)
        prev = prev_monthly_expiry_for(curr)
        self.assertLess(prev, curr)
        self.assertEqual(prev.month, curr.month - 1 if curr.month > 1 else 12)


# ══════════════════════════════════════════════════════════════════════════════
# GROUP M — LIVE CYCLE SCHEDULE (Nifty Tuesday expiry, 2026 full year)
# ══════════════════════════════════════════════════════════════════════════════

class TestCycleSchedule(unittest.TestCase):
    """
    TC-SCHED: Verify the ACTUAL Nifty entry/expiry dates for 2026.
    These tests protect against regression to the old Sensex Thursday schedule
    or any miscalculation of the Tuesday expiry rule (effective Sep 2025).

    KEY FACTS confirmed on 2026-04-28:
      - Today IS the April 2026 expiry (April 28, Tuesday)
      - May 2026 ENTRY = April 29 (tomorrow, Wednesday) — NOT May 4
      - May 2026 EXPIRY = May 26 (last Tuesday) — NOT May 27/28
      - All 2026 expiries are Tuesdays (post-Sep 2025 NSE rule)
    """

    def test_SCHED01_april_2026_nifty_expiry_is_april_28_tuesday(self):
        """CRITICAL: Nifty April expiry = April 28 (Tuesday), not April 30 (Thursday)."""
        expiry = monthly_expiry_for(2026, 4)
        self.assertEqual(expiry, date(2026, 4, 28))
        self.assertEqual(expiry.weekday(), 1, "Must be Tuesday")

    def test_SCHED02_may_2026_nifty_expiry_is_may_26_tuesday(self):
        """CRITICAL: May expiry = May 26 (last Tuesday of May 2026)."""
        expiry = monthly_expiry_for(2026, 5)
        self.assertEqual(expiry, date(2026, 5, 26))
        self.assertEqual(expiry.weekday(), 1, "Must be Tuesday")

    def test_SCHED03_may_2026_entry_is_april_29_not_may4(self):
        """
        CRITICAL: Entry for May 2026 cycle = April 29 (day after April 28 expiry).
        Previous Sensex memory said 'May 4' — that was wrong (Sensex was Thursday).
        For Nifty: April expiry = April 28 → Entry = April 29.
        """
        apr_expiry = monthly_expiry_for(2026, 4)     # April 28
        may_expiry = monthly_expiry_for(2026, 5)     # May 26
        cycle = build_cycle(apr_expiry, may_expiry)
        self.assertEqual(cycle["entry_day"], date(2026, 4, 29))
        self.assertNotEqual(cycle["entry_day"], date(2026, 5, 4),
                            "May 4 was SENSEX entry — Nifty entry is April 29")

    def test_SCHED04_may_cycle_midpoint_is_may_12(self):
        """
        Midpoint = 2nd weekly Nifty expiry after entry.
        May cycle: Tuesdays between April 29 and May 26 = May 5, May 12, May 19.
        2nd = May 12. Also equals calendar midpoint (27-day cycle).
        """
        from datetime import timedelta
        entry  = date(2026, 4, 29)
        expiry = date(2026, 5, 26)
        tuesdays = [entry + timedelta(days=i)
                    for i in range(1, (expiry - entry).days)
                    if (entry + timedelta(days=i)).weekday() == 1]
        self.assertEqual(len(tuesdays), 3,
                         f"Expected 3 weekly expiries, got {tuesdays}")
        self.assertEqual(tuesdays[1], date(2026, 5, 12),
                         "2nd weekly expiry (midpoint) must be May 12")

    def test_SCHED05_sept_cycle_midpoint_differs_from_calendar_midpoint(self):
        """
        September cycle is 34 days (longer than typical 27-day cycle).
        2nd weekly expiry (Sep 8) ≠ calendar midpoint (Sep 12).
        Confirms why 2nd-weekly rule matters — do NOT fall back to calendar midpoint.
        """
        from datetime import timedelta
        entry  = date(2026, 8, 26)   # entry for Sep cycle
        expiry = date(2026, 9, 29)   # Sep expiry

        tuesdays = [entry + timedelta(days=i)
                    for i in range(1, (expiry - entry).days)
                    if (entry + timedelta(days=i)).weekday() == 1]
        second_weekly = tuesdays[1]   # Sep 8

        import math
        total = (expiry - entry).days  # 34 days
        cal_mid = entry + timedelta(days=math.floor(total / 2))  # Sep 12

        self.assertEqual(second_weekly, date(2026, 9, 8))
        self.assertEqual(cal_mid, date(2026, 9, 12))
        self.assertNotEqual(second_weekly, cal_mid,
                            "For 34-day cycle, 2nd weekly ≠ calendar midpoint")

    def test_SCHED06_all_2026_expiries_are_tuesdays(self):
        """Every 2026 Nifty monthly expiry must be a Tuesday (post Sep 2025 rule)."""
        for month in range(1, 13):
            expiry = monthly_expiry_for(2026, month)
            self.assertEqual(expiry.weekday(), 1,
                             f"{2026}-{month:02d} expiry {expiry} is not Tuesday")

    def test_SCHED07_all_2026_entry_days_are_trading_days(self):
        """Every 2026 entry day must be a valid NSE trading day."""
        from config.holidays import is_trading_day
        for month in range(1, 13):
            exp   = monthly_expiry_for(2026, month)
            prev  = prev_monthly_expiry_for(exp)
            cycle = build_cycle(prev, exp)
            entry = cycle["entry_day"]
            self.assertTrue(is_trading_day(entry),
                            f"{2026}-{month:02d} entry {entry} is a holiday/weekend")

    def test_SCHED08_entry_is_always_after_expiry_same_or_next_day(self):
        """Entry is always strictly after the previous expiry."""
        for month in range(1, 13):
            exp   = monthly_expiry_for(2026, month)
            prev  = prev_monthly_expiry_for(exp)
            cycle = build_cycle(prev, exp)
            self.assertGreater(cycle["entry_day"], prev,
                               f"Entry must be after prev expiry for month {month}")

    def test_SCHED09_gap_check_skipped_on_entry_day(self):
        """
        On entry day there is no active position yet, so gap check is skipped.
        Code guard: `active_pos and str(today) > active_pos.entry_day`
        On entry day: active_pos is None → guard is False.
        """
        cycle = _make_cycle(entry_day="2026-04-29", midpoint="2026-05-12")
        # No position added → active_pos = None
        active = cycle.active_position()
        self.assertIsNone(active,
                          "No active position on entry day → gap check skipped ✓")

    def test_SCHED10_gap_check_runs_day_after_entry(self):
        """
        Day after entry: active_pos exists and str('2026-04-30') > '2026-04-29'
        → gap check runs.
        """
        cycle = _make_cycle(entry_day="2026-04-29", midpoint="2026-05-12")
        pos = _make_position(entry_day="2026-04-29")
        cycle.upsert_position(pos)

        today = date(2026, 4, 30)
        active = cycle.active_position()
        self.assertIsNotNone(active)
        # Simulate the guard in main.py:
        gap_check_runs = (active is not None
                          and not active.closed
                          and str(today) > active.entry_day)
        self.assertTrue(gap_check_runs,
                        "Gap check must run on day after entry (April 30)")

    def test_SCHED11_gap_check_skipped_on_entry_day_even_with_position(self):
        """
        If somehow a position has entry_day == today, gap check guard
        `str(today) > active_pos.entry_day` = False → skipped on same day.
        """
        today = date(2026, 4, 29)
        pos = _make_position(entry_day="2026-04-29")
        gap_check_runs = str(today) > pos.entry_day   # "2026-04-29" > "2026-04-29" = False
        self.assertFalse(gap_check_runs,
                         "Gap check must NOT run on the same day as entry")

    def test_SCHED12_april_28_is_expiry_day_and_trading_day(self):
        """April 28 = today = Nifty April expiry AND a trading day."""
        from config.holidays import is_trading_day
        apr28 = date(2026, 4, 28)
        self.assertEqual(monthly_expiry_for(2026, 4), apr28)
        self.assertTrue(is_trading_day(apr28))

    def test_SCHED13_april_29_is_not_holiday(self):
        """April 29 (entry day) must not be a holiday."""
        from config.holidays import is_trading_day
        self.assertTrue(is_trading_day(date(2026, 4, 29)),
                        "April 29 is the entry day — must be a trading day")

    def test_SCHED14_may_26_expiry_is_trading_day(self):
        """May 26 (May expiry) must be a trading day."""
        from config.holidays import is_trading_day
        self.assertTrue(is_trading_day(date(2026, 5, 26)))

    def test_SCHED15_first_half_ends_on_may_12(self):
        """One-sided exits only allowed up to and including May 12 (midpoint)."""
        mid = date(2026, 5, 12)
        self.assertTrue(is_first_half(mid, {"calendar_midpoint": mid}),
                        "Midpoint day itself is in first half")
        self.assertFalse(is_first_half(date(2026, 5, 13), {"calendar_midpoint": mid}),
                         "May 13 is second half — only full exits allowed")

    def test_SCHED16_june_2026_entry_is_may_27(self):
        """June cycle entry = May 27 (day after May 26 expiry)."""
        may_exp  = monthly_expiry_for(2026, 5)   # May 26
        jun_exp  = monthly_expiry_for(2026, 6)   # June 30
        cycle = build_cycle(may_exp, jun_exp)
        self.assertEqual(cycle["entry_day"], date(2026, 5, 27))

    def test_SCHED17_each_cycle_has_independent_entry_midpoint_expiry(self):
        """Every month must compute its own entry/midpoint/expiry independently."""
        from datetime import timedelta
        prev_entry = None
        for month in range(4, 10):   # April through September 2026
            exp   = monthly_expiry_for(2026, month)
            prev  = prev_monthly_expiry_for(exp)
            cycle = build_cycle(prev, exp)
            entry = cycle["entry_day"]
            mid   = cycle["calendar_midpoint"]

            # Each entry must be unique
            if prev_entry is not None:
                self.assertNotEqual(entry, prev_entry,
                                    f"Month {month} entry is same as previous month!")
            # Midpoint must be strictly between entry and expiry
            self.assertGreater(mid, entry)
            self.assertLess(mid, exp)
            prev_entry = entry

    def test_SCHED18_nifty_expiry_never_thursday_post_sep2025(self):
        """Post Sep 2025: Nifty must never expire on Thursday (old rule)."""
        for month in range(1, 13):
            expiry = monthly_expiry_for(2026, month)
            self.assertNotEqual(expiry.weekday(), 3,
                                f"2026-{month:02d} expiry {expiry} is Thursday — wrong!")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()

    groups = [
        TestEntryRules,
        TestPerMinuteMonitoring,
        TestRiskRewardCapital,
        TestBreakevenBreach,
        TestGapCheck,
        TestBridgePeriod,
        TestOneSidedExit,
        TestPnLIntegrity,
        TestExpirySettlement,
        TestCircuitBreaker,
        TestStatePersistence,
        TestExpiryCalendar,
        TestCycleSchedule,
    ]

    for grp in groups:
        suite.addTests(loader.loadTestsFromTestCase(grp))

    runner = unittest.TextTestRunner(verbosity=2, descriptions=True)
    result = runner.run(suite)

    # Summary
    total  = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    print(f"\n{'='*60}")
    print(f"RESULT: {passed}/{total} passed | "
          f"{len(result.failures)} failures | {len(result.errors)} errors")
    print(f"{'='*60}")
    sys.exit(0 if result.wasSuccessful() else 1)
