"""
Indian market hours utility.

Controls when the bot should:
- Accept new signals (9:15 AM - 3:15 PM, weekdays only)
- Auto square-off positions (3:20 PM)
- Ignore ticks for strategy (after 3:30 PM or weekends/holidays)
"""

from datetime import datetime, date, time as dtime


# Market timing
MARKET_OPEN = dtime(9, 15)
LAST_ENTRY = dtime(15, 15)      # No new positions after this
SQUARE_OFF = dtime(15, 20)      # Auto-close all positions
MARKET_CLOSE = dtime(15, 30)    # Stop processing ticks for strategy

# 2026 NSE holidays (update annually)
# Source: https://www.nseindia.com/resources/exchange-communication-holidays
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 26),   # Maha Shivaratri
    date(2026, 3, 10),   # Holi
    date(2026, 3, 30),   # Id-Ul-Fitr (Ramadan)
    date(2026, 4, 2),    # Ram Navami
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 25),   # Buddha Purnima
    date(2026, 6, 5),    # Id-Ul-Adha (Bakri Id)
    date(2026, 7, 6),    # Muharram
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 19),   # Janmashtami
    date(2026, 9, 4),    # Milad-Un-Nabi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 10, 21),  # Dussehra (additional)
    date(2026, 11, 9),   # Diwali (Laxmi Pujan)
    date(2026, 11, 10),  # Diwali (Balipratipada)
    date(2026, 11, 27),  # Guru Nanak Jayanti
    date(2026, 12, 25),  # Christmas
}


def is_trading_day() -> bool:
    """Is today a trading day? (Not weekend, not holiday)"""
    today = date.today()
    # Weekend check (Saturday=5, Sunday=6)
    if today.weekday() >= 5:
        return False
    # Holiday check
    if today in NSE_HOLIDAYS_2026:
        return False
    return True


def is_market_open() -> bool:
    """Is the market currently open for trading?"""
    if not is_trading_day():
        return False
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def can_open_new_position() -> bool:
    """Can we open new positions? (No new entries after 3:15 PM)"""
    if not is_trading_day():
        return False
    now = datetime.now().time()
    return MARKET_OPEN <= now <= LAST_ENTRY


def should_square_off() -> bool:
    """Should we auto-close all positions? (At 3:20 PM)"""
    if not is_trading_day():
        return False
    now = datetime.now().time()
    return now >= SQUARE_OFF


def should_process_for_strategy() -> bool:
    """Should ticks be processed by the strategy engine?"""
    if not is_trading_day():
        return False
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE
