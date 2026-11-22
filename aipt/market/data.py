"""Market data with explicit timestamps.

Every price carries the time it was observed, and every provider answers "as of now" from
the shared clock: a point-in-time provider never returns a bar that ends after the clock,
which is what keeps replays and backtests free of look-ahead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from aipt.clock import Clock
from aipt.models import InstrumentInfo, Quote

logger = logging.getLogger(__name__)

BAR_SECONDS = {"1m": 60, "1d": 86400}
ET = ZoneInfo("America/New_York")


class MarketDataError(RuntimeError):
    pass


class MarketData(Protocol):
    def quote(self, ticker: str) -> Quote: ...
    def adv_shares(self, ticker: str, lookback: int = 20) -> float | None: ...
    def daily_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame: ...
    def dividends(self, ticker: str, start: date, end: date) -> pd.Series: ...
    def instrument_info(self, ticker: str) -> InstrumentInfo: ...


def _normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "adj_close", "volume"])
    out = frame.rename(columns={c: c.lower().replace(" ", "_") for c in frame.columns})
    if "adj_close" not in out.columns:
        out["adj_close"] = out["close"]
    idx = pd.DatetimeIndex(out.index)
    out.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return out[["open", "high", "low", "close", "adj_close", "volume"]].sort_index()


# ---------------------------------------------------------------------------
# Yahoo Finance (live)
# ---------------------------------------------------------------------------


class YahooMarketData:
    """Live data from Yahoo Finance via yfinance. Quotes are the latest 1-minute bar."""

    def __init__(self, clock: Clock):
        import yfinance as yf

        self._yf = yf
        self.clock = clock
        self._info_cache: dict[str, InstrumentInfo] = {}

    def quote(self, ticker: str) -> Quote:
        bars = _normalize_bars(self._yf.Ticker(ticker).history(period="5d", interval="1m", prepost=False,
                                                               auto_adjust=False))
        now = self.clock.now()
        if bars.empty:
            raise MarketDataError(f"no intraday bars for {ticker}")
        bar_start = bars.index[-1].to_pydatetime()
        observed = min(bar_start + timedelta(seconds=60), now)
        return Quote(ticker=ticker, price=float(bars["close"].iloc[-1]), timestamp=observed, source="yahoo:1m")

    def adv_shares(self, ticker: str, lookback: int = 20) -> float | None:
        today = self.clock.now().date()
        bars = self.daily_bars(ticker, today - timedelta(days=lookback * 2 + 10), today - timedelta(days=1))
        vol = bars["volume"].tail(lookback)
        return float(vol.mean()) if len(vol) else None

    def daily_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        frame = self._yf.Ticker(ticker).history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                                interval="1d", auto_adjust=False, actions=False)
        bars = _normalize_bars(frame)
        # Yahoo stamps daily bars at midnight exchange time; key them by session date.
        bars.index = pd.DatetimeIndex([pd.Timestamp(ts.date(), tz="UTC") for ts in
                                       pd.DatetimeIndex(frame.index).tz_localize(None)]) if len(frame) else bars.index
        return bars

    def dividends(self, ticker: str, start: date, end: date) -> pd.Series:
        series = self._yf.Ticker(ticker).dividends
        if series is None or series.empty:
            return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
        idx = pd.DatetimeIndex(series.index).tz_localize(None).normalize()
        series = pd.Series(series.values, index=idx)
        return series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]

    def instrument_info(self, ticker: str) -> InstrumentInfo:
        if ticker not in self._info_cache:
            info = self._yf.Ticker(ticker).info or {}
            self._info_cache[ticker] = InstrumentInfo(
                ticker=ticker, quote_type=info.get("quoteType"), exchange=info.get("exchange"),
                name=info.get("longName") or info.get("shortName"), currency=info.get("currency"))
        return self._info_cache[ticker]


# ---------------------------------------------------------------------------
# Point-in-time (replay and backtest)
# ---------------------------------------------------------------------------


@dataclass
class BarStore:
    """Preloaded bars keyed by (ticker, interval). Daily bars are indexed by session date at 00:00 UTC."""

    bars: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    dividends_by_ticker: dict[str, pd.Series] = field(default_factory=dict)
    info: dict[str, InstrumentInfo] = field(default_factory=dict)

    def put(self, ticker: str, interval: str, frame: pd.DataFrame) -> None:
        self.bars[(ticker, interval)] = _normalize_bars(frame)

    def get(self, ticker: str, interval: str) -> pd.DataFrame:
        try:
            return self.bars[(ticker, interval)]
        except KeyError as exc:
            raise MarketDataError(f"no {interval} bars loaded for {ticker}") from exc

    @classmethod
    def from_yahoo(cls, tickers: list[str], start: date, end: date, intraday: bool = False,
                   intraday_start: date | None = None) -> BarStore:
        """Daily bars from ``start`` (less a warm-up); 1m bars, when asked, from ``intraday_start``
        (Yahoo serves only the last ~30 days of 1m data, at most 8 days per request)."""
        import yfinance as yf

        store = cls()
        for ticker in dict.fromkeys(tickers):
            t = yf.Ticker(ticker)
            daily = t.history(start=(start - timedelta(days=45)).isoformat(), end=(end + timedelta(days=1)).isoformat(),
                              interval="1d", auto_adjust=False, actions=False)
            frame = _normalize_bars(daily)
            frame.index = pd.DatetimeIndex([pd.Timestamp(ts.date(), tz="UTC")
                                            for ts in pd.DatetimeIndex(daily.index).tz_localize(None)])
            store.bars[(ticker, "1d")] = frame
            if intraday:
                minute = t.history(start=(intraday_start or end).isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                   interval="1m", prepost=False, auto_adjust=False, actions=False)
                store.put(ticker, "1m", minute)
            divs = t.dividends
            if divs is not None and not divs.empty:
                idx = pd.DatetimeIndex(divs.index).tz_localize(None).normalize()
                store.dividends_by_ticker[ticker] = pd.Series(divs.values, index=idx)
            info = {}
            try:
                info = t.info or {}
            except Exception as exc:  # metadata is best effort; validation fails closed without it
                logger.warning("instrument info unavailable for %s: %s", ticker, exc)
            store.info[ticker] = InstrumentInfo(ticker=ticker, quote_type=info.get("quoteType"),
                                                exchange=info.get("exchange"),
                                                name=info.get("longName") or info.get("shortName"),
                                                currency=info.get("currency"))
        return store


class PointInTimeMarketData:
    """Answers from a BarStore as of the clock. A bar is visible once it has ended.

    Intraday quotes use 1-minute bars when loaded; otherwise the latest *completed* daily
    close (a daily bar ends at that session's 16:00 ET close).
    """

    def __init__(self, store: BarStore, clock: Clock, calendar=None):
        from aipt.market.calendar import USEquityCalendar

        self.store = store
        self.clock = clock
        self.calendar = calendar or USEquityCalendar()

    def _visible_daily(self, ticker: str) -> pd.DataFrame:
        bars = self.store.get(ticker, "1d")
        now = self.clock.now()
        keep = [self.calendar.session_close(ts.date()) <= now if self.calendar.is_trading_day(ts.date()) else False
                for ts in bars.index]
        return bars[keep]

    def quote(self, ticker: str) -> Quote:
        now = self.clock.now()
        minute = self.store.bars.get((ticker, "1m"))
        if minute is not None and not minute.empty:
            ended = minute[minute.index + pd.Timedelta(seconds=60) <= now]
            if not ended.empty:
                ts = ended.index[-1].to_pydatetime() + timedelta(seconds=60)
                return Quote(ticker=ticker, price=float(ended["close"].iloc[-1]), timestamp=ts, source="pit:1m")
        today = now.astimezone(ET).date()
        if self.calendar.is_trading_day(today):
            opens, closes = self.calendar.session_open(today), self.calendar.session_close(today)
            bars = self.store.get(ticker, "1d")
            row = bars[bars.index == pd.Timestamp(today, tz="UTC")]
            if opens <= now < closes and not row.empty:
                # Without minute bars, the only price known intraday is the official open.
                return Quote(ticker=ticker, price=float(row["open"].iloc[0]), timestamp=opens, source="pit:1d-open")
        daily = self._visible_daily(ticker)
        if daily.empty:
            raise MarketDataError(f"no {ticker} price visible at {now.isoformat()}")
        day = daily.index[-1].date()
        return Quote(ticker=ticker, price=float(daily["close"].iloc[-1]), timestamp=self.calendar.session_close(day),
                     source="pit:1d-close")

    def next_open(self, ticker: str, after: datetime) -> Quote:
        """The first traded price at or after ``after``: next 1m bar open, else next session open."""
        minute = self.store.bars.get((ticker, "1m"))
        if minute is not None and not minute.empty:
            later = minute[minute.index >= pd.Timestamp(after)]
            if not later.empty:
                return Quote(ticker=ticker, price=float(later["open"].iloc[0]),
                             timestamp=later.index[0].to_pydatetime(), source="pit:1m-open")
        daily = self.store.get(ticker, "1d")
        for ts, row in daily.iterrows():
            opens = self.calendar.session_open(ts.date()) if self.calendar.is_trading_day(ts.date()) else None
            if opens is not None and opens >= after:
                return Quote(ticker=ticker, price=float(row["open"]), timestamp=opens, source="pit:1d-open")
        raise MarketDataError(f"no {ticker} open after {after.isoformat()}")

    def adv_shares(self, ticker: str, lookback: int = 20) -> float | None:
        vol = self._visible_daily(ticker)["volume"].tail(lookback)
        return float(vol.mean()) if len(vol) else None

    def daily_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        bars = self._visible_daily(ticker)
        return bars[(bars.index >= pd.Timestamp(start, tz="UTC")) & (bars.index <= pd.Timestamp(end, tz="UTC"))]

    def dividends(self, ticker: str, start: date, end: date) -> pd.Series:
        series = self.store.dividends_by_ticker.get(ticker)
        if series is None or series.empty:
            return pd.Series(dtype=float, index=pd.DatetimeIndex([]))
        visible_end = min(end, self.clock.now().date())
        return series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(visible_end))]

    def instrument_info(self, ticker: str) -> InstrumentInfo:
        return self.store.info.get(ticker) or InstrumentInfo(ticker=ticker)


# ---------------------------------------------------------------------------
# QuantDinger Agent Gateway quotes
# ---------------------------------------------------------------------------


class QuantDingerMarketData:
    """Quotes from QuantDinger's /klines (1m bars carry a timestamp; /price does not).

    Everything else (ADV, daily bars, dividends, instrument metadata) is delegated, since
    the Agent Gateway does not serve instrument metadata or corporate actions.
    """

    def __init__(self, client, clock: Clock, fallback: MarketData, market: str = "USStock"):
        self.client = client
        self.clock = clock
        self.fallback = fallback
        self.market = market

    def quote(self, ticker: str) -> Quote:
        rows = self.client.klines(self.market, ticker, "1m", limit=1)
        if not rows:
            raise MarketDataError(f"QuantDinger returned no 1m bar for {ticker}")
        bar = rows[-1]
        observed = min(datetime.fromtimestamp(int(bar["time"]) + 60, UTC), self.clock.now())
        return Quote(ticker=ticker, price=float(bar["close"]), timestamp=observed, source="quantdinger:1m")

    def adv_shares(self, ticker: str, lookback: int = 20) -> float | None:
        return self.fallback.adv_shares(ticker, lookback)

    def daily_bars(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        return self.fallback.daily_bars(ticker, start, end)

    def dividends(self, ticker: str, start: date, end: date) -> pd.Series:
        return self.fallback.dividends(ticker, start, end)

    def instrument_info(self, ticker: str) -> InstrumentInfo:
        return self.fallback.instrument_info(ticker)
