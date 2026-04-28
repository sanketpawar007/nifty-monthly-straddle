"""Position dataclasses — in-memory + persisted trade state."""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Leg:
    symbol:       str
    strike:       float
    opt_type:     str    # "CE" or "PE"
    direction:    str    # "short" or "long"
    qty:          int
    entry_price:  float
    exit_price:   float = 0.0
    exited:       bool  = False
    exit_reason:  str   = ""

    def pnl_per_unit(self) -> float:
        if not self.exited:
            return 0.0
        if self.direction == "short":
            return self.entry_price - self.exit_price
        return self.exit_price - self.entry_price


@dataclass
class IronFlyPosition:
    cycle_expiry:    str
    entry_day:       str
    is_reentry:      bool  = False
    reentry_n:       int   = 0

    spot_at_entry:   float = 0.0
    atm_strike:      float = 0.0
    wing_dist:       float = 0.0
    net_credit:      float = 0.0
    upper_be:        float = 0.0
    lower_be:        float = 0.0
    entry_timestamp: str   = ""

    short_ce: Optional[Leg] = None
    short_pe: Optional[Leg] = None
    long_ce:  Optional[Leg] = None
    long_pe:  Optional[Leg] = None

    ce_exited: bool  = False
    pe_exited: bool  = False

    # v4: extra opposite-side spread added on BE breach before 1st weekly expiry
    extra_short_pe: Optional[Leg] = None   # added when upper BE breached
    extra_long_pe:  Optional[Leg] = None
    extra_short_ce: Optional[Leg] = None   # added when lower BE breached
    extra_long_ce:  Optional[Leg] = None
    be_reentry_done:     bool  = False
    crystallized_pnl_rs: float = 0.0

    margin_blocked_rs: float = 0.0
    sl_trigger_rs:     float = 0.0

    closed:         bool  = False
    exit_timestamp: str   = ""
    exit_reason:    str   = ""
    gross_pnl_rs:   float = 0.0
    entry_cost_rs:  float = 0.0
    exit_cost_rs:   float = 0.0
    net_pnl_rs:     float = 0.0

    def active_legs(self) -> list:
        legs = []
        if not self.ce_exited:
            if self.short_ce: legs.append(self.short_ce)
            if self.long_ce:  legs.append(self.long_ce)
        if not self.pe_exited:
            if self.short_pe: legs.append(self.short_pe)
            if self.long_pe:  legs.append(self.long_pe)
        # v4 extra opposite-side legs
        for xl in [self.extra_short_pe, self.extra_long_pe,
                   self.extra_short_ce, self.extra_long_ce]:
            if xl: legs.append(xl)
        return [l for l in legs if not l.exited]

    def active_net_credit(self) -> float:
        credit = 0.0
        if not self.ce_exited and self.short_ce and self.long_ce:
            credit += self.short_ce.entry_price - self.long_ce.entry_price
        if not self.pe_exited and self.short_pe and self.long_pe:
            credit += self.short_pe.entry_price - self.long_pe.entry_price
        return credit

    def all_symbols(self) -> list:
        return [leg.symbol for leg in [
            self.short_ce, self.short_pe, self.long_ce, self.long_pe,
            self.extra_short_pe, self.extra_long_pe,
            self.extra_short_ce, self.extra_long_ce,
        ] if leg]

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "IronFlyPosition":
        _leg_attrs = ("short_ce", "short_pe", "long_ce", "long_pe",
                      "extra_short_pe", "extra_long_pe",
                      "extra_short_ce", "extra_long_ce")
        pos = IronFlyPosition(**{k: v for k, v in d.items() if k not in _leg_attrs})
        for attr in _leg_attrs:
            if d.get(attr):
                setattr(pos, attr, Leg(**d[attr]))
        return pos


@dataclass
class CycleState:
    monthly_expiry:   str
    entry_day:        str
    calendar_midpoint: str
    first_weekly_expiry: str   = ""
    reentry_count:    int   = 0
    reentry_cap:      int   = 1
    bridge_threshold: float = 0.01
    gap_open_price:   float = 0.0
    positions:        list  = field(default_factory=list)
    status:           str   = "WAITING"

    def active_position(self) -> Optional[IronFlyPosition]:
        for pd in reversed(self.positions):
            pos = IronFlyPosition.from_dict(pd)
            if not pos.closed:
                return pos
        return None

    def upsert_position(self, pos: IronFlyPosition):
        pd = pos.to_dict()
        for i, existing in enumerate(self.positions):
            if existing.get("entry_timestamp") == pd.get("entry_timestamp"):
                self.positions[i] = pd
                return
        self.positions.append(pd)

    def to_dict(self) -> dict:
        return {
            "monthly_expiry":    self.monthly_expiry,
            "entry_day":         self.entry_day,
            "calendar_midpoint":   self.calendar_midpoint,
            "first_weekly_expiry": self.first_weekly_expiry,
            "reentry_count":       self.reentry_count,
            "reentry_cap":       self.reentry_cap,
            "bridge_threshold":  self.bridge_threshold,
            "gap_open_price":    self.gap_open_price,
            "positions":         self.positions,
            "status":            self.status,
        }

    @staticmethod
    def from_dict(d: dict) -> "CycleState":
        return CycleState(
            monthly_expiry    = d["monthly_expiry"],
            entry_day         = d["entry_day"],
            calendar_midpoint   = d["calendar_midpoint"],
            first_weekly_expiry = d.get("first_weekly_expiry", ""),
            reentry_count       = d.get("reentry_count", 0),
            reentry_cap       = d.get("reentry_cap", 1),
            bridge_threshold  = d.get("bridge_threshold", 0.01),
            gap_open_price    = d.get("gap_open_price", 0.0),
            positions         = d.get("positions", []),
            status            = d.get("status", "WAITING"),
        )
