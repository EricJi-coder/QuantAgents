"""US regular-session calendar (NYSE, XNYS) via QuantDinger's market_schedule module."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from aipt import vendor
from aipt.models import SessionInfo

ET = ZoneInfo("America/New_York")
MARKET = "USStock"


class USEquityCalendar:
    def __init__(self):
        self._schedule = vendor.market_schedule()
        self._cal = self._schedule._calendar(MARKET)

    def session_for(self, now: datetime) -> SessionInfo:
        """The session on ``now``'s New York date (open or not)."""
        day = now.astimezone(ET).date()
        label = pd.Timestamp(day)
        if not self._cal.is_session(label):
            return SessionInfo(session_date=day, is_open=False, opens_at=None, closes_at=None)
        opens = self._cal.session_open(label).to_pydatetime()
        closes = self._cal.session_close(label).to_pydatetime()
        return SessionInfo(session_date=day, is_open=self._schedule.is_equity_market_open(MARKET, now),
                           opens_at=opens, closes_at=closes)

    def is_trading_day(self, day: date) -> bool:
        return bool(self._cal.is_session(pd.Timestamp(day)))

    def within_trading_window(self, now: datetime, open_buffer_min: int, close_buffer_min: int) -> tuple[bool, str]:
        info = self.session_for(now)
        if info.opens_at is None:
            return False, f"{info.session_date} is not a NYSE trading day"
        if not info.is_open:
            return False, f"regular session closed at {now.astimezone(ET):%Y-%m-%d %H:%M %Z}"
        if now < info.opens_at + timedelta(minutes=open_buffer_min):
            return False, f"within {open_buffer_min} min of the open"
        if now > info.closes_at - timedelta(minutes=close_buffer_min):
            return False, f"within {close_buffer_min} min of the close"
        return True, "regular session"

    def sessions_between(self, start: date, end: date) -> list[date]:
        return [ts.date() for ts in self._cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]

    def next_session(self, day: date) -> date:
        return self._cal.next_session(pd.Timestamp(day)).date() if self.is_trading_day(day) else \
            self._cal.date_to_session(pd.Timestamp(day), direction="next").date()

    def previous_session(self, day: date) -> date:
        return self._cal.previous_session(pd.Timestamp(day)).date() if self.is_trading_day(day) else \
            self._cal.date_to_session(pd.Timestamp(day), direction="previous").date()

    def session_open(self, day: date) -> datetime:
        return self._cal.session_open(pd.Timestamp(day)).to_pydatetime()

    def session_close(self, day: date) -> datetime:
        return self._cal.session_close(pd.Timestamp(day)).to_pydatetime()
