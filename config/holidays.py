"""NSE trading holidays. Update each year before Jan 1."""
from datetime import date

NSE_HOLIDAYS: set[date] = {
    # 2022
    date(2022, 1, 26), date(2022, 3, 1),  date(2022, 3, 18), date(2022, 4, 14),
    date(2022, 4, 15), date(2022, 5, 3),  date(2022, 8, 9),  date(2022, 8, 15),
    date(2022, 10, 2), date(2022, 10, 5), date(2022, 10, 24),date(2022, 10, 26),
    date(2022, 11, 8),
    # 2023
    date(2023, 1, 26), date(2023, 3, 7),  date(2023, 3, 30), date(2023, 4, 4),
    date(2023, 4, 14), date(2023, 4, 21), date(2023, 5, 1),  date(2023, 6, 28),
    date(2023, 8, 15), date(2023, 9, 19), date(2023, 10, 2), date(2023, 10, 24),
    date(2023, 11, 14),date(2023, 11, 27),date(2023, 12, 25),
    # 2024
    date(2024, 1, 22), date(2024, 3, 25), date(2024, 3, 29), date(2024, 4, 14),
    date(2024, 4, 17), date(2024, 5, 23), date(2024, 6, 17), date(2024, 7, 17),
    date(2024, 8, 15), date(2024, 10, 2), date(2024, 11, 1), date(2024, 11, 15),
    date(2024, 12, 25),
    # 2025
    date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31), date(2025, 4, 10),
    date(2025, 4, 14), date(2025, 4, 18), date(2025, 5, 1),  date(2025, 8, 15),
    date(2025, 8, 27), date(2025, 10, 2), date(2025, 10, 21),date(2025, 10, 24),
    date(2025, 11, 5), date(2025, 12, 25),
    # 2026 — full NSE calendar (source: NSE circular / BSE holiday list)
    date(2026, 1, 26),                    # Republic Day
    date(2026, 3, 20),                    # Holi (2nd day)
    date(2026, 4, 2),                     # Ram Navami / Good Friday (provisional)
    date(2026, 4, 3),                     # Good Friday
    date(2026, 4, 14),                    # Dr. Ambedkar Jayanti / Baisakhi
    date(2026, 5, 1),                     # Maharashtra Day
    date(2026, 8, 15),                    # Independence Day
    date(2026, 10, 2),                    # Gandhi Jayanti
    date(2026, 10, 21),                   # Diwali-Laxmi Puja (Muhurat — provisional)
    date(2026, 10, 22),                   # Diwali Balipratipada (provisional)
    date(2026, 11, 20),                   # Guru Nanak Jayanti (provisional)
    date(2026, 12, 25),                   # Christmas
}


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def next_trading_day(d: date) -> date:
    from datetime import timedelta
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d
