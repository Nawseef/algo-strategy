"""
Forex / CFD trading-hours utility (the CFD counterpart of market_hours.py).

The NSE ``market_hours.py`` module is unchanged and still drives the Groww/NSE
system. This module handles the very different CFD/forex clock:

  * The market is ~24/5. It opens Sunday and closes Friday, both pinned to
    New York 17:00 (5pm ET). The weekend gap is [Fri 17:00 ET .. Sun 17:00 ET].
  * There is no single "market open" bell; instead there are overlapping FX
    sessions (Sydney / Tokyo / London / New York). A strategy typically only
    trades around specific session opens (e.g. London or New York).

DST is handled correctly by using the IANA timezone database via ``zoneinfo``
(stdlib, Python 3.9+): NY / London / Tokyo / Sydney each observe their own DST
rules, so we define each session by its *local* clock and let zoneinfo convert.
This avoids hardcoded UTC hours that would drift an hour twice a year.

All computation is in real UTC. Both VMs run IST, but every function uses
tz-aware datetimes (``datetime.now(timezone.utc)``), so the host's local
timezone is irrelevant.

Config via env (all optional):
  FX_TRADING_SESSIONS            comma list of sessions allowed for entries
                                 (default "london,new_york")
  FX_FLATTEN_BEFORE_WEEKEND_MIN  minutes before Friday close to stop opening /
                                 flatten positions (default 60)
  FX_DAILY_RESET_HOUR            hour of the firm's daily drawdown reset (default 17)
  FX_DAILY_RESET_TZ              timezone for that hour (default America/New_York)
  FX_FLATTEN_BEFORE_RESET_MIN    minutes before the daily reset to flatten (default 5)
"""

from __future__ import annotations

import os
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

# ─── Timezones ───────────────────────────────────────────────
_NY = ZoneInfo("America/New_York")
_LONDON = ZoneInfo("Europe/London")
_TOKYO = ZoneInfo("Asia/Tokyo")
_SYDNEY = ZoneInfo("Australia/Sydney")

# ─── The forex week ──────────────────────────────────────────
# Opens Sunday 17:00 ET, closes Friday 17:00 ET (both DST-aware via _NY).
_WEEK_ANCHOR = dtime(17, 0)

# ─── FX sessions: (zone, local_open, local_close) ────────────
# Defined in each market's *local* time so DST is automatic. These are the
# commonly cited session hours; tweak here if you prefer different bounds.
SESSIONS: dict[str, tuple[ZoneInfo, dtime, dtime]] = {
    "sydney": (_SYDNEY, dtime(7, 0), dtime(16, 0)),
    "tokyo": (_TOKYO, dtime(9, 0), dtime(18, 0)),
    "london": (_LONDON, dtime(8, 0), dtime(17, 0)),
    "new_york": (_NY, dtime(8, 0), dtime(17, 0)),
}

# ─── Full-closure holidays (UTC date). FX is thin/halted on these. ──
# IC Markets halts around Christmas and New Year's Day.
FX_HOLIDAYS = {
    (12, 25),  # Christmas
    (1, 1),    # New Year's Day
}


# ─── Config helpers ──────────────────────────────────────────
def _configured_sessions() -> list[str]:
    raw = os.getenv("FX_TRADING_SESSIONS", "london,new_york")
    out = [s.strip().lower() for s in raw.split(",") if s.strip()]
    return [s for s in out if s in SESSIONS] or ["london", "new_york"]


def _flatten_before_weekend_min() -> int:
    try:
        return int(os.getenv("FX_FLATTEN_BEFORE_WEEKEND_MIN", "60"))
    except ValueError:
        return 60


def _reset_hour() -> int:
    """Hour (in the reset timezone) of the firm's daily drawdown reset."""
    try:
        return int(os.getenv("FX_DAILY_RESET_HOUR", "17"))
    except ValueError:
        return 17


def _reset_tz() -> ZoneInfo:
    """Timezone the daily reset hour is expressed in (default US Eastern)."""
    try:
        return ZoneInfo(os.getenv("FX_DAILY_RESET_TZ", "America/New_York"))
    except Exception:  # noqa: BLE001 - bad tz name -> safe default
        return _NY


def _flatten_before_reset_min() -> int:
    try:
        return int(os.getenv("FX_FLATTEN_BEFORE_RESET_MIN", "5"))
    except ValueError:
        return 5


def _now(now: datetime | None) -> datetime:
    """Normalize an input to a tz-aware UTC datetime (defaults to real now)."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        # Treat naive input as UTC (never as host-local, which is IST here).
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


# ─── Weekly open / close ─────────────────────────────────────
def is_holiday(now: datetime | None = None) -> bool:
    """Is the given instant on a full-closure FX holiday (UTC date)?"""
    n = _now(now)
    return (n.month, n.day) in FX_HOLIDAYS


def is_market_open(now: datetime | None = None) -> bool:
    """Is the forex market open? (Not in the weekend gap, not a holiday.)

    Weekend gap = Friday 17:00 ET .. Sunday 17:00 ET (DST-aware).
    """
    n = _now(now)
    if is_holiday(n):
        return False
    ny = n.astimezone(_NY)
    wd = ny.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    t = ny.time()
    if wd == 5:  # Saturday: always closed
        return False
    if wd == 4 and t >= _WEEK_ANCHOR:  # Friday after 17:00 ET
        return False
    if wd == 6 and t < _WEEK_ANCHOR:   # Sunday before 17:00 ET
        return False
    return True


def _next_open_dt(n: datetime) -> datetime:
    """UTC datetime of the next Sunday 17:00 ET open at/after ``n``."""
    ny = n.astimezone(_NY)
    # Find the next Sunday (weekday 6) on/after today.
    days_ahead = (6 - ny.weekday()) % 7
    candidate_date = (ny + timedelta(days=days_ahead)).date()
    open_ny = datetime.combine(candidate_date, _WEEK_ANCHOR, tzinfo=_NY)
    if open_ny <= ny:
        # This Sunday's open already passed — go to next week.
        open_ny = datetime.combine(candidate_date + timedelta(days=7), _WEEK_ANCHOR, tzinfo=_NY)
    return open_ny.astimezone(timezone.utc)


def seconds_until_market_open(now: datetime | None = None) -> float:
    """Seconds until the next weekly open. 0 if the market is already open."""
    n = _now(now)
    if is_market_open(n):
        return 0.0
    return max(0.0, (_next_open_dt(n) - n).total_seconds())


def _friday_close_dt(n: datetime) -> datetime:
    """UTC datetime of the Friday 17:00 ET close for the current FX week."""
    ny = n.astimezone(_NY)
    # Days until Friday (weekday 4). If it's Sat/Sun, the relevant close already
    # happened; callers only use this while the market is open, so this returns
    # the upcoming Friday close within the current week.
    days_ahead = (4 - ny.weekday()) % 7
    close_date = (ny + timedelta(days=days_ahead)).date()
    close_ny = datetime.combine(close_date, _WEEK_ANCHOR, tzinfo=_NY)
    if close_ny < ny:
        close_ny = datetime.combine(close_date + timedelta(days=7), _WEEK_ANCHOR, tzinfo=_NY)
    return close_ny.astimezone(timezone.utc)


# ─── Sessions ────────────────────────────────────────────────
def is_session_active(session: str, now: datetime | None = None) -> bool:
    """Is the named FX session currently in its local trading hours?"""
    session = session.lower()
    if session not in SESSIONS:
        return False
    if not is_market_open(now):
        return False
    zone, open_t, close_t = SESSIONS[session]
    local = _now(now).astimezone(zone)
    # Sessions here don't cross local midnight, so a simple range works.
    return open_t <= local.time() < close_t


def active_sessions(now: datetime | None = None) -> list[str]:
    """List of FX sessions currently active (empty if market closed)."""
    if not is_market_open(now):
        return []
    return [s for s in SESSIONS if is_session_active(s, now)]


def session_tag(now: datetime | None = None) -> str:
    """Compact session label for a candle: e.g. 'london', 'tokyo+london', 'off'.

    Sessions overlap, so this is a '+'-joined set. Filter in SQL with LIKE,
    e.g. WHERE session LIKE '%london%'. 'off' = market open but between the
    named session windows; 'closed' = market closed.
    """
    if not is_market_open(now):
        return "closed"
    active = active_sessions(now)
    return "+".join(active) if active else "off"


def trading_day(now: datetime | None = None) -> str:
    """FX trading-day date (YYYY-MM-DD), rolling at 17:00 New York.

    The forex day does not roll at UTC midnight — it rolls at the 17:00 ET
    weekly/daily boundary (the same anchor used for the weekend gap). The
    Sunday 17:00 ET -> Monday 17:00 ET session is labelled 'Monday', etc. Use
    this instead of the UTC calendar date for any day-scoped logic (opening
    range, daily limits, per-day aggregation).
    """
    ny = _now(now).astimezone(_NY)
    d = ny.date()
    if ny.time() >= _WEEK_ANCHOR:  # at/after 17:00 ET -> belongs to next FX day
        d = d + timedelta(days=1)
    return d.isoformat()


# ─── Strategy gating (mirrors the NSE market_hours interface) ──
def should_process_for_strategy(now: datetime | None = None) -> bool:
    """Should ticks/candles be processed by the strategy engine? (Market open.)"""
    return is_market_open(now)


def in_trading_window(now: datetime | None = None) -> bool:
    """Is now inside at least one of the FX_TRADING_SESSIONS windows?"""
    if not is_market_open(now):
        return False
    wanted = _configured_sessions()
    return any(is_session_active(s, now) for s in wanted)


def should_flatten_before_weekend(
    now: datetime | None = None, minutes: int | None = None
) -> bool:
    """True in the last ``minutes`` before the Friday weekly close.

    CFDs carry weekend-gap risk and overnight swap, so an intraday/session
    strategy should flatten before the week closes.
    """
    n = _now(now)
    if not is_market_open(n):
        return False
    window = _flatten_before_weekend_min() if minutes is None else minutes
    close = _friday_close_dt(n)
    return timedelta(0) <= (close - n) <= timedelta(minutes=window)


def _next_daily_reset_dt(n: datetime) -> datetime:
    """UTC datetime of the next daily reset at/after ``n``.

    The reset instant is the firm's daily drawdown-reset time (FX_DAILY_RESET_HOUR
    in FX_DAILY_RESET_TZ; default 17:00 America/New_York, the common 5pm ET roll).
    Firm-agnostic — override via env once a prop firm is chosen.
    """
    local = n.astimezone(_reset_tz())
    reset = local.replace(hour=_reset_hour(), minute=0, second=0, microsecond=0)
    if local >= reset:
        reset = reset + timedelta(days=1)
    return reset.astimezone(timezone.utc)


def seconds_until_daily_reset(now: datetime | None = None) -> float:
    """Seconds until the next daily reset."""
    n = _now(now)
    return max(0.0, (_next_daily_reset_dt(n) - n).total_seconds())


def should_flatten_before_daily_reset(
    now: datetime | None = None, minutes: int | None = None
) -> bool:
    """True in the last ``minutes`` before the daily reset (while market open).

    Counterpart to should_flatten_before_weekend for intraday accounts whose
    daily drawdown re-baselines at end-of-day. Swing/multi-day strategies simply
    don't call this. Off by default in the sense that nothing invokes it until
    the execution flatten-guard is built.
    """
    n = _now(now)
    if not is_market_open(n):
        return False
    window = _flatten_before_reset_min() if minutes is None else minutes
    nxt = _next_daily_reset_dt(n)
    return timedelta(0) <= (nxt - n) <= timedelta(minutes=window)


def can_open_new_position(now: datetime | None = None) -> bool:
    """Can we open a new position now?

    Requires: market open AND inside a configured trading session AND not in the
    pre-weekend flatten window.
    """
    return (
        in_trading_window(now)
        and not should_flatten_before_weekend(now)
    )


def status(now: datetime | None = None) -> dict[str, object]:
    """Snapshot of the current forex clock (handy for logging / debugging)."""
    n = _now(now)
    open_ = is_market_open(n)
    return {
        "utc": n.isoformat(timespec="seconds"),
        "market_open": open_,
        "active_sessions": active_sessions(n),
        "trading_sessions": _configured_sessions(),
        "in_trading_window": in_trading_window(n),
        "can_open": can_open_new_position(n),
        "flatten_soon": should_flatten_before_weekend(n),
        "secs_to_open": round(seconds_until_market_open(n)) if not open_ else 0,
    }


if __name__ == "__main__":
    # Manual check: python -m app.utils.forex_hours
    import json

    print(json.dumps(status(), indent=2))
