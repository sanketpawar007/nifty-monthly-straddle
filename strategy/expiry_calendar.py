"""
Cycle definition (v3 Section 2) — Nifty monthly expiry calendar.

SEBI Rule Changes:
  Pre-Sep 2025  : monthly expiry = last Thursday of month
  Sep 2025+     : monthly expiry = last Tuesday  of month (NSE circular Sept 2025)
"""
import math
import calendar as _calendar
from datetime import date, timedelta
from typing import Optional

from config.holidays import is_trading_day, next_trading_day

# NSE changed Nifty monthly expiry from Thursday to Tuesday effective Sep 2025
_TUESDAY_EXPIRY_FROM = date(2025, 9, 1)


def last_thursday_of_month(year: int, month: int) -> date:
    last_day = date(year, month, _calendar.monthrange(year, month)[1])
    d = last_day
    while d.weekday() != 3:   # 3 = Thursday
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def last_tuesday_of_month(year: int, month: int) -> date:
    last_day = date(year, month, _calendar.monthrange(year, month)[1])
    d = last_day
    while d.weekday() != 1:   # 1 = Tuesday
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def monthly_expiry_for(year: int, month: int) -> date:
    """Nifty monthly expiry for given year/month — handles Thursday→Tuesday transition."""
    ref = date(year, month, 1)
    if ref >= _TUESDAY_EXPIRY_FROM:
        return last_tuesday_of_month(year, month)
    return last_thursday_of_month(year, month)


def prev_monthly_expiry_for(expiry: date) -> date:
    """Return the previous monthly expiry before the given expiry date."""
    if expiry.month == 1:
        return monthly_expiry_for(expiry.year - 1, 12)
    return monthly_expiry_for(expiry.year, expiry.month - 1)


def calendar_midpoint(entry_day: date, monthly_expiry: date) -> date:
    total = (monthly_expiry - entry_day).days
    return entry_day + timedelta(days=math.floor(total / 2))


def first_trading_day_after(expiry: date) -> date:
    return next_trading_day(expiry)


def build_cycle(prev_monthly_expiry: date, monthly_expiry: date) -> dict:
    entry_day = first_trading_day_after(prev_monthly_expiry)
    mid       = calendar_midpoint(entry_day, monthly_expiry)
    return {
        "prev_expiry":       prev_monthly_expiry,
        "monthly_expiry":    monthly_expiry,
        "entry_day":         entry_day,
        "calendar_midpoint": mid,
    }


def is_first_half(current_date: date, cycle: dict) -> bool:
    return current_date <= cycle["calendar_midpoint"]


def is_entry_day(today: date, cycle: dict) -> bool:
    return today == cycle["entry_day"]


def is_expiry_day(today: date, cycle: dict) -> bool:
    return today == cycle["monthly_expiry"]


def round_half_up(value: float, step: int = 50) -> float:
    """Round to nearest step using round-half-up (v3 §3.2)."""
    return math.floor(value / step + 0.5) * step
