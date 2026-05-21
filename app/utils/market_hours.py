"""
Indian market hours utility.

Controls when the bot should:
- Accept new signals (9:15 AM - 3:15 PM, weekdays only)
- Auto square-off positions (3:20 PM)
- Ignore ticks for strategy (after 3:30 PM or weekends/holidays)
- Sleep until next market open (pre-market warmup window)
"""

from datetime import datetime, date, time as dtime, timedelta


# Market timing
MARKET_OPEN = dtime(9, 15)
LAST_ENTRY = dtime(15, 15)      # No new positions after this
SQUARE_OFF = dtime(15, 20)      # Auto-close all positions
MARKET_CLOSE = dtime(15, 30)    # Stop processing ticks for strategy

# Pre-market: bot wakes up early to authenticate + warmup before 9:15
PRE_MARKET_WAKE = dtime(9, 0)   # 15 min before market open

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


def is_trading_day(target_date: date | None = None) -> bool:
    """Is the given date (or today) a trading day? (Not weekend, not holiday)"""
    today = target_date or date.today()
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


def is_within_active_window() -> bool:
    """
    Is the bot within the active window where it should connect to feeds?

    Active window: 9:05 AM to 3:30 PM on trading days.
    Outside this window, the bot should sleep instead of connecting.
    """
    if not is_trading_day():
        return False
    now = datetime.now().time()
    return PRE_MARKET_WAKE <= now <= MARKET_CLOSE


def seconds_until_market_open() -> float:
    """
    Calculate seconds until the next pre-market wake time (9:05 AM on next trading day).

    Returns 0 if we're currently within the active window.
    """
    now = datetime.now()
    today = now.date()
    current_time = now.time()

    # If today is a trading day and we're before market close
    if is_trading_day(today) and current_time < MARKET_CLOSE:
        if current_time >= PRE_MARKET_WAKE:
            # Already in active window
            return 0.0
        else:
            # Today is trading day but too early (e.g., 3 AM)
            wake_dt = datetime.combine(today, PRE_MARKET_WAKE)
            return (wake_dt - now).total_seconds()

    # Market closed for today — find next trading day
    next_day = today + timedelta(days=1)
    for _ in range(10):  # Max 10 days lookahead
        if is_trading_day(next_day):
            wake_dt = datetime.combine(next_day, PRE_MARKET_WAKE)
            return (wake_dt - now).total_seconds()
        next_day += timedelta(days=1)

    # Fallback: sleep 12 hours
    return 12 * 3600
