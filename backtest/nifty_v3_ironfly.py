#!/usr/bin/env python3
"""
Nifty Monthly Iron Butterfly Backtest — v3
==========================================
Period  : Oct 2022 – Apr 2026  (41 usable months, real 1-min NSE/NFO data)
Data    : NFO 1-min parquet files, configured via NIFTY_DATA_DIR env var

SEBI Rule Changes Applied
--------------------------
| Effective          | Change                                            |
|--------------------|---------------------------------------------------|
| Pre-Sep 2025       | Monthly expiry = last Thursday of month           |
| Sep 2025 onwards   | Monthly expiry = last Tuesday  of month (NSE)     |
| Pre-Nov 20 2024    | Nifty lot size = 75 units                         |
| Nov 20 2024 onwards| Nifty lot size = 65 units (SEBI rationalization)  |

Strategy Rules (v3)
-------------------
1. Entry  : 1st trading day of month at 11:00 AM IST on the monthly-expiry contract
2. Legs   : Sell ATM Call (K) + Sell ATM Put (K)  [K = nearest 50-pt strike to spot]
            NC_sell = ATM_CE_entry + ATM_PE_entry
            wing_dist = round(NC_sell / 50) * 50  (nearest 50-pt, min 50)
            Buy CE at K + wing_dist, Buy PE at K − wing_dist
3. NET_NC = NC_sell − (buy_CE_entry + buy_PE_entry)   [per unit; true net credit]
4. Target  : Combined MTM P&L ≥ +50% × NET_NC × units  → full exit
   SL      : No hard SL before re-entry window; effective SL only via re-entry
5. Re-entry: If SL threshold hit before calendar midpoint, square off ALL legs,
             re-enter with new ATM on SAME expiry at current market.
             One re-entry allowed per cycle.
6. Partial : If spot drifts to ≥ (NET_NC + BUFFER_PTS) away from ATM on one side
             AND SL has not hit, exit ONLY that side (2 legs).
             No SL on the remaining leg — target only.
7. Hard exit: Full exit at 15:28 IST on expiry day.

Configure data path:
  export NIFTY_DATA_DIR=/path/to/nfo/processed/NIFTY
  python3 backtest/nifty_v3_ironfly.py
"""

import os
import sys
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional, List

import pandas as pd
import pytz

# ── Data directory ─────────────────────────────────────────────────────────────
PROCESSED_DIR = Path(os.getenv("NIFTY_DATA_DIR", "/data/nfo/processed/NIFTY"))

# ── Constants ──────────────────────────────────────────────────────────────────
IST         = pytz.timezone("Asia/Kolkata")
STRIKE_STEP = 50

LOT_SIZE_OLD  = 75
LOT_SIZE_NEW  = 65
LOT_CHANGE_DT = date(2024, 11, 20)

LOTS = int(os.getenv("LOTS", "1"))

TARGET_PCT   = 0.50
SL_PCT       = 9.99    # effectively no SL before re-entry window
PARTIAL_FRAC = 1.00
BUFFER_PTS   = 50

SLIPPAGE  = 0.50
BROKERAGE = 20.0
STT_SELL  = 0.000625
EXCHANGE  = 0.00053
GST_RATE  = 0.18
STAMP     = 0.00003

# ── NSE Holidays ───────────────────────────────────────────────────────────────
NSE_HOLIDAYS = {
    date(2022,1,26),date(2022,3,1),date(2022,3,18),date(2022,4,14),
    date(2022,4,15),date(2022,5,3),date(2022,8,9),date(2022,8,15),
    date(2022,10,2),date(2022,10,5),date(2022,10,24),date(2022,10,26),
    date(2022,11,8),
    date(2023,1,26),date(2023,3,7),date(2023,3,30),date(2023,4,4),
    date(2023,4,14),date(2023,4,21),date(2023,5,1),date(2023,6,28),
    date(2023,8,15),date(2023,9,19),date(2023,10,2),date(2023,10,24),
    date(2023,11,14),date(2023,11,27),date(2023,12,25),
    date(2024,1,22),date(2024,3,25),date(2024,3,29),date(2024,4,14),
    date(2024,4,17),date(2024,5,23),date(2024,6,17),date(2024,7,17),
    date(2024,8,15),date(2024,10,2),date(2024,11,1),date(2024,11,15),
    date(2024,12,25),
    date(2025,2,26),date(2025,3,14),date(2025,3,31),date(2025,4,10),
    date(2025,4,14),date(2025,4,18),date(2025,5,1),date(2025,8,15),
    date(2025,8,27),date(2025,10,2),date(2025,10,21),date(2025,10,24),
    date(2025,11,5),date(2025,12,25),
    date(2026,1,26),date(2026,3,20),date(2026,4,2),date(2026,4,3),
    date(2026,4,14),date(2026,5,1),
}

# ── Verified monthly expiry map ────────────────────────────────────────────────
MONTHLY_EXPIRIES = {
    "2022-10": date(2022,10,27), "2022-11": date(2022,11,24),
    "2022-12": date(2022,12,29), "2023-01": date(2023,1,25),
    "2023-02": date(2023,2,23),  "2023-03": date(2023,3,29),
    "2023-04": date(2023,4,27),  "2023-06": date(2023,6,29),
    "2023-07": date(2023,7,27),  "2023-08": date(2023,8,31),
    "2023-09": date(2023,9,28),  "2023-10": date(2023,10,26),
    "2023-11": date(2023,11,30), "2023-12": date(2023,12,28),
    "2024-01": date(2024,1,25),  "2024-02": date(2024,2,29),
    "2024-03": date(2024,3,28),  "2024-04": date(2024,4,25),
    "2024-06": date(2024,6,27),  "2024-07": date(2024,7,25),
    "2024-08": date(2024,8,29),  "2024-09": date(2024,9,26),
    "2024-10": date(2024,10,31), "2024-11": date(2024,11,28),
    "2024-12": date(2024,12,26), "2025-01": date(2025,1,30),
    "2025-02": date(2025,2,27),  "2025-03": date(2025,3,27),
    "2025-04": date(2025,4,30),  "2025-05": date(2025,5,29),
    "2025-06": date(2025,6,26),  "2025-07": date(2025,7,31),
    "2025-08": date(2025,8,28),  "2025-09": date(2025,9,30),
    "2025-10": date(2025,10,28), "2025-11": date(2025,11,25),
    "2025-12": date(2025,12,30), "2026-01": date(2026,1,27),
    "2026-02": date(2026,2,24),  "2026-03": date(2026,3,30),
    "2026-04": date(2026,4,28),
}

# ── Spot data cache (optional — loaded from pkl if available) ──────────────────
_SPOT_CACHE = None
_SPOT_PKL   = os.getenv("NIFTY_SPOT_PKL", "")


def _load_spot_cache():
    global _SPOT_CACHE
    if _SPOT_CACHE is not None:
        return
    if _SPOT_PKL and Path(_SPOT_PKL).exists():
        df = pd.read_pickle(_SPOT_PKL)
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(IST)
        _SPOT_CACHE = df.set_index("date").sort_index()


def _sc(ts) -> Optional[float]:
    _load_spot_cache()
    if _SPOT_CACHE is None:
        return None
    i = _SPOT_CACHE.index.searchsorted(ts, side="right") - 1
    return float(_SPOT_CACHE.iloc[i]["close"]) if i >= 0 else None


def _so(ts) -> Optional[float]:
    _load_spot_cache()
    if _SPOT_CACHE is None:
        return None
    r = _SPOT_CACHE[_SPOT_CACHE.index == ts]
    return float(r.iloc[0]["open"]) if not r.empty else _sc(ts)


# ── Calendar helpers ───────────────────────────────────────────────────────────

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def first_trading_day(yr: int, mo: int) -> date:
    d = date(yr, mo, 1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def trading_days_in_range(d1: date, d2: date) -> List[date]:
    result, cur = [], d1
    while cur <= d2:
        if is_trading_day(cur):
            result.append(cur)
        cur += timedelta(days=1)
    return result


def lot_size_on(d: date) -> int:
    return LOT_SIZE_NEW if d >= LOT_CHANGE_DT else LOT_SIZE_OLD


# ── Parquet / option-data helpers ──────────────────────────────────────────────

_day_cache: dict = {}


def load_day(d: date) -> pd.DataFrame:
    if d in _day_cache:
        return _day_cache[d]
    p = PROCESSED_DIR / f"{d}.parquet"
    if not p.exists():
        _day_cache[d] = pd.DataFrame()
        return _day_cache[d]
    df = pd.read_parquet(p)
    df["date"]   = pd.to_datetime(df["date"], utc=True).dt.tz_convert(IST)
    df["strike"] = df["strike"].astype(float)
    _day_cache[d] = df
    if len(_day_cache) > 3:
        oldest = next(iter(_day_cache))
        del _day_cache[oldest]
    return df


def get_price_at(d: date, expiry: date, strike: float,
                 opt_type: str, ts: datetime) -> Optional[float]:
    df = load_day(d)
    if df.empty:
        return None
    mask = ((df["expiry"] == expiry) &
            (df["strike"] == strike) &
            (df["instrument_type"] == opt_type))
    sub  = df[mask]
    if sub.empty:
        return None
    sub = sub[sub["date"] <= ts]
    return float(sub.iloc[-1]["close"]) if not sub.empty else None


# ── Cost helper ────────────────────────────────────────────────────────────────

def transaction_cost(nc_sell: float, buy_debit: float, lot_size: int, n_roundtrips: int = 1) -> float:
    units    = lot_size
    sell_val = nc_sell  * units
    buy_val  = buy_debit * units
    brok     = BROKERAGE * 4 * 2 * n_roundtrips
    stt      = STT_SELL * sell_val * 2 * n_roundtrips
    exch     = EXCHANGE * (sell_val + buy_val) * 2 * n_roundtrips
    gst      = GST_RATE * (brok + exch)
    stamp    = STAMP * buy_val * 2 * n_roundtrips
    return brok + stt + exch + gst + stamp


# ── Trade result ───────────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    ym:           str
    entry_date:   date
    expiry_date:  date
    exit_date:    date
    exit_time:    str
    lot_size:     int
    atm:          int
    wing_dist:    int
    nc_sell:      float
    net_nc:       float
    exit_type:    str
    re_entry:     bool
    pnl_gross:    float
    cost_total:   float
    pnl_net:      float
    capital_reqd: float
    roi_pct:      float
    locked_pnl:   float
    rem_net_nc:   float
    surviving_leg_profit:  float
    surviving_leg_capital: float
    surviving_leg_roi:     float


# ── Cycle simulator ────────────────────────────────────────────────────────────

def simulate_cycle(ym: str, entry_day_override=None) -> Optional[TradeResult]:
    yr, mo    = int(ym[:4]), int(ym[5:7])
    expiry    = MONTHLY_EXPIRIES[ym]
    entry_day = entry_day_override if entry_day_override else first_trading_day(yr, mo)

    while load_day(entry_day).empty and entry_day < expiry:
        entry_day += timedelta(days=1)
        while not is_trading_day(entry_day):
            entry_day += timedelta(days=1)

    lot_sz = lot_size_on(entry_day)
    units  = LOTS * lot_sz
    mid_cal = entry_day + (expiry - entry_day) / 2

    entry_ts = IST.localize(datetime(entry_day.year, entry_day.month, entry_day.day, 11, 0))
    df_entry = load_day(entry_day)
    if df_entry.empty:
        return None

    sub_entry = df_entry[(df_entry["expiry"] == expiry) &
                          (df_entry["instrument_type"].isin(["CE", "PE"]))]
    sub_entry = sub_entry[sub_entry["date"] <= entry_ts]
    if sub_entry.empty:
        return None

    chain = sub_entry.groupby(["strike", "instrument_type"])["close"].last().unstack()
    if "CE" not in chain.columns or "PE" not in chain.columns:
        return None
    chain = chain.dropna()
    if chain.empty:
        return None

    _sv = _sc(entry_ts)
    if _sv:
        atm_k = int(round(_sv / STRIKE_STEP) * STRIKE_STEP)
    else:
        chain["diff"] = (chain["CE"] - chain["PE"]).abs()
        atm_k = round(int(chain["diff"].idxmin()) / STRIKE_STEP) * STRIKE_STEP

    sc_px = get_price_at(entry_day, expiry, float(atm_k), "CE", entry_ts)
    sp_px = get_price_at(entry_day, expiry, float(atm_k), "PE", entry_ts)
    if sc_px is None or sp_px is None or sc_px < 1 or sp_px < 1:
        return None

    sc_px += SLIPPAGE
    sp_px += SLIPPAGE

    nc_sell   = sc_px + sp_px
    wing_dist = max(STRIKE_STEP, round(nc_sell / STRIKE_STEP) * STRIKE_STEP)

    bc_strike = atm_k + wing_dist
    bp_strike = atm_k - wing_dist

    bc_px = (get_price_at(entry_day, expiry, float(bc_strike), "CE", entry_ts) or 0.0) + SLIPPAGE
    bp_px = (get_price_at(entry_day, expiry, float(bp_strike), "PE", entry_ts) or 0.0) + SLIPPAGE

    buy_debit = bc_px + bp_px
    net_nc    = nc_sell - buy_debit

    if net_nc <= 0:
        return None

    max_loss_per_unit = wing_dist - net_nc
    capital_reqd      = max(max_loss_per_unit, net_nc) * units

    target_pnl = +TARGET_PCT * net_nc * units
    sl_pnl     = -SL_PCT     * net_nc * units
    total_cost = transaction_cost(nc_sell, buy_debit, lot_sz)

    print(f"  [{ym}] entry={entry_day} exp={expiry} ({expiry.strftime('%a')})"
          f" ATM={atm_k} wing={wing_dist} NC_sell={nc_sell:.1f}"
          f" NET_NC={net_nc:.1f} LotSz={lot_sz}"
          f" TP=₹{target_pnl:,.0f}")

    re_entry_done  = False
    partial_done   = False
    call_active    = True
    put_active     = True
    locked_pnl_at_partial = 0.0
    rem_net_nc_at_partial  = 0.0

    cur_atm    = atm_k
    cur_wing   = wing_dist
    cur_sc_px  = sc_px
    cur_sp_px  = sp_px
    cur_bc_px  = bc_px
    cur_bp_px  = bp_px
    cur_net_nc = net_nc
    cur_entry_ts = entry_ts
    locked_pnl   = 0.0

    last_sc = sc_px
    last_sp = sp_px
    last_bc = bc_px
    last_bp = bp_px

    exit_result = None
    pnl_unit    = 0.0

    all_days = trading_days_in_range(entry_day, expiry)

    for tday in all_days:
        df_day = load_day(tday)
        if df_day.empty:
            continue

        # Gap check at 9:15 (days after entry, first half, re-entry eligible)
        if (call_active or put_active) and tday > entry_day:
            _t9  = IST.localize(datetime(tday.year, tday.month, tday.day, 9, 15))
            _t9ts = pd.Timestamp(_t9)
            _go  = _so(_t9ts)
            _fh9 = tday <= mid_cal
            _ube = cur_atm + cur_net_nc
            _lbe = cur_atm - cur_net_nc
            if _go and (_go >= _ube or _go <= _lbe):
                _xsc = get_price_at(tday, expiry, float(cur_atm), "CE", _t9ts) or last_sc
                _xsp = get_price_at(tday, expiry, float(cur_atm), "PE", _t9ts) or last_sp
                _xbc = get_price_at(tday, expiry, float(cur_atm + cur_wing), "CE", _t9ts) or last_bc
                _xbp = get_price_at(tday, expiry, float(cur_atm - cur_wing), "PE", _t9ts) or last_bp
                _pu  = ((cur_sc_px - _xsc) + (cur_sp_px - _xsp) + (_xbc - cur_bc_px) + (_xbp - cur_bp_px))
                _prs = min(max(locked_pnl + _pu * units, -(cur_wing - cur_net_nc) * units), cur_net_nc * units)
                _et  = "GAP_UP" if _go >= _ube else "GAP_DOWN"
                if not _fh9 or re_entry_done:
                    exit_result = (_et, tday, time(9, 15), _prs, _xsc)
                    break
                locked_pnl = _prs
                # Bridge check: spot must stay within ±1% of gap_open during 9:15→11:00
                _t1100ts = pd.Timestamp(IST.localize(datetime(tday.year, tday.month, tday.day, 11, 0)))
                if _SPOT_CACHE is not None:
                    _win = _SPOT_CACHE[(_SPOT_CACHE.index >= _t9ts) & (_SPOT_CACHE.index <= _t1100ts)]["close"]
                    if not _win.empty and (_win - _go).abs().max() / _go > 0.01:
                        exit_result = (_et + "+BRIDGE", tday, time(9, 15), _prs, _xsc)
                        break
                _ts_re  = _t1100ts
                _re_sv  = _sc(_ts_re)
                _ra = int(round(_re_sv / STRIKE_STEP) * STRIKE_STEP) if _re_sv else None
                _rs0 = get_price_at(tday, expiry, float(_ra), "CE", _ts_re) if _ra else None
                _rp0 = get_price_at(tday, expiry, float(_ra), "PE", _ts_re) if _ra else None
                if not _ra or not _rs0 or not _rp0 or _rs0 < 0.5 or _rp0 < 0.5:
                    exit_result = (_et + "+REENTRY_FAIL", tday, time(9, 15), _prs, _xsc)
                    break
                _rsc = _rs0 - SLIPPAGE; _rsp = _rp0 - SLIPPAGE
                _rncs = _rs0 + _rp0
                _rw   = max(round(_rncs / STRIKE_STEP) * STRIKE_STEP, STRIKE_STEP)
                _rlc0 = get_price_at(tday, expiry, float(_ra + _rw), "CE", _ts_re) or 0.05
                _rlp0 = get_price_at(tday, expiry, float(_ra - _rw), "PE", _ts_re) or 0.05
                _rlc  = _rlc0 + SLIPPAGE; _rlp = _rlp0 + SLIPPAGE
                _rnet = _rsc + _rsp - _rlc - _rlp
                if _rnet <= 0:
                    exit_result = (_et + "+NO_CREDIT", tday, time(9, 15), _prs, _xsc)
                    break
                cur_atm = _ra; cur_wing = _rw
                cur_sc_px = _rsc; cur_sp_px = _rsp; cur_bc_px = _rlc; cur_bp_px = _rlp
                last_sc = _rsc; last_sp = _rsp; last_bc = _rlc; last_bp = _rlp
                cur_net_nc = _rnet; call_active = True; put_active = True; re_entry_done = True
                cur_entry_ts = _ts_re
                target_pnl = locked_pnl + TARGET_PCT * _rnet * units
                sl_pnl     = float("-inf")
                total_cost += transaction_cost(_rncs, _rlc + _rlp, lot_sz) + SLIPPAGE * units * 4

        mask_exp = df_day["expiry"] == expiry
        df_exp   = df_day[mask_exp]

        def leg_prices(df_exp, strike, opt_type):
            sub = df_exp[(df_exp["strike"] == float(strike)) &
                         (df_exp["instrument_type"] == opt_type)]
            if sub.empty:
                return pd.Series(dtype=float)
            return sub.set_index("date")["close"].sort_index()

        sc_ser = leg_prices(df_exp, cur_atm,            "CE")
        sp_ser = leg_prices(df_exp, cur_atm,            "PE")
        bc_ser = leg_prices(df_exp, cur_atm + cur_wing, "CE")
        bp_ser = leg_prices(df_exp, cur_atm - cur_wing, "PE")

        all_idx = sorted(set(sc_ser.index) | set(sp_ser.index) |
                         set(bc_ser.index) | set(bp_ser.index))

        for ts in all_idx:
            tod = ts.to_pydatetime().astimezone(IST)
            t   = tod.time()
            if t < time(9, 15) or t > time(15, 30):
                continue
            if tod < cur_entry_ts.astimezone(IST):
                continue

            def _last(ser, key):
                v = ser.get(key)
                if v is None: return None
                return float(v.iloc[-1]) if hasattr(v, "iloc") else float(v)

            if ts in sc_ser.index: last_sc = _last(sc_ser, ts) or last_sc
            if ts in sp_ser.index: last_sp = _last(sp_ser, ts) or last_sp
            if ts in bc_ser.index: last_bc = _last(bc_ser, ts) or last_bc
            if ts in bp_ser.index: last_bp = _last(bp_ser, ts) or last_bp

            pnl_unit = 0.0
            if call_active:
                pnl_unit += (cur_sc_px - last_sc) + (last_bc - cur_bc_px)
            if put_active:
                pnl_unit += (cur_sp_px - last_sp) + (last_bp - cur_bp_px)

            pnl_rs = locked_pnl + pnl_unit * units

            # Hard exit at expiry close (15:28)
            if tday == expiry and t >= time(15, 28):
                exit_result = ("HARD_EXIT", tday, t, pnl_rs, last_sc)
                break

            if pnl_rs >= target_pnl:
                exit_result = ("TARGET", tday, t, pnl_rs, last_sc)
                break

            if pnl_rs <= sl_pnl:
                if not re_entry_done and tday <= mid_cal:
                    re_entry_done = True
                    locked_pnl    = pnl_rs
                    print(f"    Re-entry @ {tday} {t} locked=₹{locked_pnl:,.0f}")
                    df_now = df_day[(df_day["expiry"] == expiry) &
                                    (df_day["instrument_type"].isin(["CE", "PE"]))]
                    df_now = df_now[df_now["date"] <= ts]
                    if df_now.empty:
                        exit_result = ("SL", tday, t, pnl_rs, last_sc); break
                    ch2 = df_now.groupby(["strike", "instrument_type"])["close"].last().unstack().dropna()
                    if "CE" not in ch2.columns or "PE" not in ch2.columns:
                        exit_result = ("SL", tday, t, pnl_rs, last_sc); break
                    ch2["diff"] = (ch2["CE"] - ch2["PE"]).abs()
                    new_atm = round(int(ch2["diff"].idxmin()) / STRIKE_STEP) * STRIKE_STEP
                    sc2 = ch2.loc[float(new_atm), "CE"] if float(new_atm) in ch2.index else None
                    sp2 = ch2.loc[float(new_atm), "PE"] if float(new_atm) in ch2.index else None
                    if sc2 is None or sp2 is None:
                        exit_result = ("SL", tday, t, pnl_rs, last_sc); break
                    sc2 += SLIPPAGE; sp2 += SLIPPAGE
                    nc2  = sc2 + sp2
                    wd2  = max(STRIKE_STEP, round(nc2 / STRIKE_STEP) * STRIKE_STEP)
                    bc2  = float(ch2.loc[float(new_atm + wd2), "CE"]) + SLIPPAGE if float(new_atm + wd2) in ch2.index else SLIPPAGE
                    bp2  = float(ch2.loc[float(new_atm - wd2), "PE"]) + SLIPPAGE if float(new_atm - wd2) in ch2.index else SLIPPAGE
                    nn2  = nc2 - bc2 - bp2
                    if nn2 <= 0:
                        exit_result = ("SL", tday, t, pnl_rs, last_sc); break
                    cur_atm = new_atm; cur_wing = wd2
                    cur_sc_px = sc2; cur_sp_px = sp2; cur_bc_px = bc2; cur_bp_px = bp2
                    cur_net_nc = nn2; call_active = True; put_active = True
                    last_sc = sc2; last_sp = sp2; last_bc = bc2; last_bp = bp2
                    cur_entry_ts = ts
                    target_pnl = locked_pnl + TARGET_PCT * nn2 * units
                    sl_pnl     = locked_pnl - SL_PCT * nn2 * units
                    total_cost += transaction_cost(nc2, bc2 + bp2, lot_sz)
                    continue
                else:
                    exit_result = ("SL", tday, t, pnl_rs, last_sc); break

            # Partial exit (first half only)
            _fh = tday <= mid_cal
            if not partial_done and call_active and put_active and _fh:
                approx_spot = _sc(ts) or (cur_atm + (last_sc - last_sp) / 2)
                upper_trigger = cur_atm + PARTIAL_FRAC * cur_net_nc + BUFFER_PTS
                lower_trigger = cur_atm - PARTIAL_FRAC * cur_net_nc - BUFFER_PTS

                if approx_spot >= upper_trigger:
                    call_pnl = (cur_sc_px - last_sc + last_bc - cur_bc_px) * units
                    locked_pnl += call_pnl
                    call_active = False; partial_done = True
                    rem_net_nc  = cur_sp_px - cur_bp_px
                    target_pnl  = locked_pnl + TARGET_PCT * rem_net_nc * units
                    sl_pnl      = float("-inf")
                    locked_pnl_at_partial = locked_pnl
                    rem_net_nc_at_partial  = rem_net_nc
                    print(f"    Partial EXIT call @ {tday} {t} locked=₹{call_pnl:,.0f}")
                elif approx_spot <= lower_trigger:
                    put_pnl = (cur_sp_px - last_sp + last_bp - cur_bp_px) * units
                    locked_pnl += put_pnl
                    put_active  = False; partial_done = True
                    rem_net_nc  = cur_sc_px - cur_bc_px
                    target_pnl  = locked_pnl + TARGET_PCT * rem_net_nc * units
                    sl_pnl      = float("-inf")
                    locked_pnl_at_partial = locked_pnl
                    rem_net_nc_at_partial  = rem_net_nc
                    print(f"    Partial EXIT put @ {tday} {t} locked=₹{put_pnl:,.0f}")

        if exit_result:
            break

    if exit_result is None:
        exit_result = ("HARD_EXIT", expiry, time(15, 29),
                       locked_pnl + pnl_unit * units, last_sc)

    etype, exit_day, exit_t, pnl_gross, _ = exit_result

    if partial_done:
        etype += "+PARTIAL_CALL" if not call_active else "+PARTIAL_PUT"
    if re_entry_done:
        etype = "REENTRY+" + etype

    pnl_net = pnl_gross - total_cost
    roi     = (pnl_net / capital_reqd * 100) if capital_reqd > 0 else 0.0

    if partial_done and rem_net_nc_at_partial > 0:
        surv_profit  = round(TARGET_PCT * rem_net_nc_at_partial * units)
        surv_capital = round((cur_wing - rem_net_nc_at_partial) * units)
        surv_roi     = round(surv_profit / surv_capital * 100, 2) if surv_capital > 0 else 0.0
    else:
        surv_profit = surv_capital = surv_roi = 0.0

    return TradeResult(
        ym=ym, entry_date=entry_day, expiry_date=expiry,
        exit_date=exit_day, exit_time=str(exit_t)[:5],
        lot_size=lot_sz, atm=cur_atm, wing_dist=cur_wing,
        nc_sell=nc_sell, net_nc=net_nc,
        exit_type=etype, re_entry=re_entry_done,
        pnl_gross=round(pnl_gross), cost_total=round(total_cost),
        pnl_net=round(pnl_net), capital_reqd=round(capital_reqd),
        roi_pct=round(roi, 2),
        locked_pnl=round(locked_pnl_at_partial),
        rem_net_nc=round(rem_net_nc_at_partial, 1),
        surviving_leg_profit=surv_profit,
        surviving_leg_capital=surv_capital,
        surviving_leg_roi=surv_roi,
    )


def main():
    print("=" * 75)
    print("  Nifty Monthly Iron Butterfly Backtest  (Real NSE 1-min data)")
    print("=" * 75)
    print()
    print(f"  Data dir : {PROCESSED_DIR}")
    print(f"  Spot pkl : {_SPOT_PKL or 'not configured (NIFTY_SPOT_PKL env var)'}")
    print( "  Lot size : 75 units  → 65 units  (effective Nov 20, 2024)")
    print( "  Expiry   : Thursday  → Tuesday   (effective Sep  01, 2025)")
    print(f"  Lots     : {LOTS}")
    print( "  Strategy : Target 50% of NET_NC | Partial exit at 100%+50pt drift")
    print()

    months = sorted(MONTHLY_EXPIRIES.keys())
    print(f"Months in scope: {len(months)}  ({months[0]} → {months[-1]})")
    print("─" * 75)

    # Build entry day map: each cycle starts the first trading day after prev expiry
    _srt  = sorted(MONTHLY_EXPIRIES.keys())
    _emap = {}
    _td2  = timedelta
    for _i2, _ym2 in enumerate(_srt):
        if _i2 == 0:
            _yr2, _mo2 = int(_ym2[:4]), int(_ym2[5:7])
            _d2 = date(_yr2, _mo2, 1)
            while not is_trading_day(_d2): _d2 += timedelta(days=1)
            _emap[_ym2] = _d2
        else:
            _pe = MONTHLY_EXPIRIES[_srt[_i2 - 1]]
            _nd = _pe + timedelta(days=1)
            while not is_trading_day(_nd): _nd += timedelta(days=1)
            _emap[_ym2] = _nd

    results: List[TradeResult] = []
    for ym in months:
        r = simulate_cycle(ym, entry_day_override=_emap.get(ym))
        if r:
            results.append(r)
        else:
            print(f"  [{ym}] SKIPPED (missing data)")

    if not results:
        print("No results.")
        return

    rows = []
    for r in results:
        rows.append({
            "Month":     r.ym,      "Entry":    str(r.entry_date),
            "Expiry":    str(r.expiry_date),     "Exp Day":  r.expiry_date.strftime("%a"),
            "Exit Date": str(r.exit_date),       "Exit Time":r.exit_time,
            "LotSz":     r.lot_size,"ATM":       r.atm,
            "Wing":      r.wing_dist,"NC_sell":  round(r.nc_sell, 1),
            "NET_NC":    round(r.net_nc, 1),     "Exit Type":r.exit_type,
            "P&L Gross": r.pnl_gross,"Costs":   r.cost_total,
            "P&L Net":   r.pnl_net, "Capital Reqd":r.capital_reqd,
            "ROI%":      r.roi_pct,
            "Locked P&L":r.locked_pnl         if r.locked_pnl          else "",
            "Rem NC":    r.rem_net_nc          if r.rem_net_nc          else "",
            "Surv Profit":r.surviving_leg_profit if r.surviving_leg_profit else "",
            "Surv Cap":  r.surviving_leg_capital if r.surviving_leg_capital else "",
            "Surv ROI%": r.surviving_leg_roi   if r.surviving_leg_roi   else "",
        })

    df = pd.DataFrame(rows)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    print()
    print("═" * 75)
    print("MONTH-BY-MONTH RESULTS")
    print("═" * 75)
    print(df.to_string(index=False))

    n       = len(results)
    winners = [r for r in results if r.pnl_net > 0]
    losers  = [r for r in results if r.pnl_net <= 0]
    targets = [r for r in results if "TARGET"    in r.exit_type]
    partials= [r for r in results if "PARTIAL"   in r.exit_type]
    reents  = [r for r in results if r.re_entry]

    total_net = sum(r.pnl_net for r in results)
    avg_cap   = sum(r.capital_reqd for r in results) / n
    max_cap   = max(r.capital_reqd for r in results)

    cur_streak = max_consec = 0
    for r in results:
        if r.pnl_net <= 0: cur_streak += 1; max_consec = max(max_consec, cur_streak)
        else: cur_streak = 0

    print()
    print("═" * 75)
    print("SUMMARY STATISTICS")
    print("═" * 75)
    print(f"  Total months   : {n}  |  Winners: {len(winners)} ({len(winners)/n*100:.1f}%)  |  Losers: {len(losers)}")
    print(f"  Max consec loss: {max_consec}")
    print(f"  Target exits   : {len(targets)}  |  Partials: {len(partials)}  |  Re-entries: {len(reents)}")
    print(f"  Total NET P&L  : ₹{total_net:,.0f}  |  Avg/month: ₹{total_net/n:,.0f}")
    print(f"  Avg capital    : ₹{avg_cap:,.0f}  |  Max capital: ₹{max_cap:,.0f}")
    print(f"  Overall ROI    : {total_net/avg_cap*100:.1f}%  (over {n} months on avg capital)")
    print(f"  Safe capital   : ₹{max_cap*1.3:,.0f}  (max × 1.3 MTM buffer)")
    print()

    out = Path(__file__).parent / "nifty_results.csv"
    df.to_csv(out, index=False)
    print(f"Results saved → {out}")


if __name__ == "__main__":
    main()
