"""Major market events that trigger entry skip (within 48 hours of entry date)."""
from datetime import date

SKIP_EVENTS = [
    # RBI MPC 2025-2026 (announcement day)
    (date(2025, 4, 9),   "RBI MPC"),
    (date(2025, 6, 6),   "RBI MPC"),
    (date(2025, 8, 6),   "RBI MPC"),
    (date(2025, 10, 8),  "RBI MPC"),
    (date(2025, 12, 5),  "RBI MPC"),
    (date(2026, 2, 6),   "RBI MPC"),
    (date(2026, 4, 3),   "RBI MPC"),
    (date(2026, 6, 5),   "RBI MPC"),
    # Union Budget
    (date(2025, 2, 1),   "Union Budget"),
    (date(2026, 2, 1),   "Union Budget"),
    # US Fed FOMC 2025-2026
    (date(2025, 3, 19),  "Fed FOMC"),
    (date(2025, 5, 7),   "Fed FOMC"),
    (date(2025, 6, 18),  "Fed FOMC"),
    (date(2025, 7, 30),  "Fed FOMC"),
    (date(2025, 9, 17),  "Fed FOMC"),
    (date(2025, 11, 5),  "Fed FOMC"),
    (date(2025, 12, 17), "Fed FOMC"),
    (date(2026, 1, 28),  "Fed FOMC"),
    (date(2026, 3, 18),  "Fed FOMC"),
    (date(2026, 5, 6),   "Fed FOMC"),
    (date(2026, 6, 17),  "Fed FOMC"),
]


def has_major_event_within_48h(entry_date: date) -> tuple:
    """Return (True, description) if a major event falls within 2 calendar days of entry_date."""
    for event_date, desc in SKIP_EVENTS:
        if abs((event_date - entry_date).days) <= 2:
            return True, f"{desc} on {event_date}"
    return False, ""
