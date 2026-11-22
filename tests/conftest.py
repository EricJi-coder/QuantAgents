"""Offline fixtures: a synthetic point-in-time market, a simulated clock, the local paper
venue, and a scripted researcher/formalizer standing in for the LLMs. Nothing here reaches
a broker, an exchange, QuantDinger or an LLM provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from aipt.clock import SimulatedClock
from aipt.config import load_settings
from aipt.execution.adapter import fill_reference_for_replay
from aipt.execution.venues import LocalPaperVenue
from aipt.ledger import Ledger
from aipt.market.calendar import USEquityCalendar
from aipt.market.data import BarStore, PointInTimeMarketData
from aipt.models import DecisionProposal, Evidence, EvidenceKind, EvidenceSource, InstrumentInfo
from aipt.workflow.cycle import TradingSystem

ET = ZoneInfo("America/New_York")
SESSION = date(2026, 11, 20)          # a Friday NYSE session
PRICES = {"AAA": 100.0, "BBB": 50.0, "CCC": 200.0, "THIN": 20.0, "SPY": 600.0, "TQQQ": 80.0}
VOLUME = {"THIN": 1_000}             # 1% of ADV = 10 shares → partial fills


def make_store(daily_drift: float = 0.001) -> BarStore:
    cal = USEquityCalendar()
    days = cal.sessions_between(SESSION - timedelta(days=200), SESSION + timedelta(days=10))
    last_idx = days.index(SESSION)
    store = BarStore()
    for ticker, price in PRICES.items():
        closes = [price * ((1 + daily_drift) ** (i - last_idx)) for i in range(len(days))]
        store.bars[(ticker, "1d")] = pd.DataFrame(
            {"open": [c * 0.999 for c in closes], "high": [c * 1.01 for c in closes],
             "low": [c * 0.99 for c in closes], "close": closes, "adj_close": closes,
             "volume": VOLUME.get(ticker, 5_000_000)},
            index=pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in days]))
        idx = pd.date_range(cal.session_open(SESSION), periods=390, freq="1min")
        store.bars[(ticker, "1m")] = pd.DataFrame(
            {"open": price, "high": price, "low": price, "close": price, "adj_close": price, "volume": 10_000},
            index=idx)
        store.info[ticker] = InstrumentInfo(
            ticker=ticker, quote_type="ETF" if ticker in ("SPY", "TQQQ") else "EQUITY",
            exchange="PCX" if ticker in ("SPY", "TQQQ") else "NMS",
            name="ProShares UltraPro QQQ" if ticker == "TQQQ" else f"{ticker} Inc.", currency="USD")
    return store


def proposal(ticker: str, action: str = "buy", target: float = 0.05, confidence: float = 0.7,
             as_of: str = "2026-11-19", fact: bool = True, **kw) -> DecisionProposal:
    evidence = [Evidence(statement=f"{ticker} revenue grew 12% year over year in the last quarter",
                         kind=EvidenceKind.VERIFIED_FACT if fact else EvidenceKind.HYPOTHESIS,
                         source=EvidenceSource.FUNDAMENTALS_REPORT, as_of=as_of),
                Evidence(statement="Margins should keep expanding", kind=EvidenceKind.HYPOTHESIS,
                         source=EvidenceSource.INVESTMENT_DEBATE)]
    return DecisionProposal(ticker=kw.pop("ticker_override", ticker), action=action, target_weight=target,
                            thesis=f"{ticker} test thesis with enough words.", evidence=evidence,
                            confidence=confidence, uncertainty="demand could slow", risks=["valuation"],
                            time_horizon="1-3 months", **kw)


@dataclass
class Script:
    """Per-ticker (rating, proposal-or-None). None mimics free-form text the formalizer could not structure."""

    entries: dict[str, tuple[str, DecisionProposal | None]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail_on: set[str] = field(default_factory=set)


class FakeResearcher:
    def __init__(self, script: Script):
        self.script = script

    def analyze(self, ticker: str, trade_date: str, cash: float, positions) -> dict[str, Any]:
        self.script.calls.append(ticker)
        if ticker in self.script.fail_on:
            raise RuntimeError("provider unavailable")
        rating, _ = self.script.entries[ticker]
        state = {"market_report": f"{ticker} market report", "sentiment_report": "sentiment",
                 "news_report": "news", "fundamentals_report": f"{ticker} revenue grew 12%",
                 "investment_debate_state": {"judge_decision": "plan"}, "investment_plan": "plan",
                 "trader_investment_plan": "trade", "risk_debate_state": {"judge_decision": "ok"},
                 "final_trade_decision": f"**Rating**: {rating}", "final_rating": rating}
        return {"final_state": state, "signal": rating, "started_at": "2026-11-20T15:00:00+00:00",
                "finished_at": "2026-11-20T15:01:00+00:00", "run_settings": {}}


class FakeFormalizer:
    def __init__(self, script: Script):
        self.script = script

    def model_name(self) -> str:
        return "fake-formalizer"

    def formalize(self, ticker, trade_date, final_state, current_weight):
        _, prop = self.script.entries[ticker]
        if prop is None:
            return None, "structured output failed: OutputParserException: free-form text"
        return prop, None


@dataclass
class Harness:
    system: TradingSystem
    clock: SimulatedClock
    script: Script
    venue: LocalPaperVenue
    ledger: Ledger
    settings: Any

    def at(self, hhmm: str, day: date = SESSION) -> None:
        h, m = (int(x) for x in hhmm.split(":"))
        self.clock.set(datetime(day.year, day.month, day.day, h, m, tzinfo=ET))


def build_harness(tmp_path: Path, overrides: dict | None = None, store: BarStore | None = None) -> Harness:
    base = {"paths": {"data_dir": str(tmp_path / "data"), "reports_dir": str(tmp_path / "reports")},
            "research": {"watchlist": ["AAA", "BBB", "CCC"], "max_tickers_per_cycle": 3},
            "execution": {"venue": "local"}}
    if overrides:
        from aipt.config import _deep_merge

        base = _deep_merge(base, overrides)
    settings = load_settings(overrides=base)
    clock = SimulatedClock(datetime(2026, 11, 20, 10, 30, tzinfo=ET))
    md = PointInTimeMarketData(store or make_store(), clock)
    venue = LocalPaperVenue(tmp_path / "data" / "venue.db",
                            fill_reference_for_replay(md, clock, settings.execution.execution_latency_seconds))
    ledger = Ledger(settings.ledger_path, settings.account.id)
    script = Script()
    system = TradingSystem(settings, clock=clock, ledger=ledger, market_data=md, venue=venue,
                           researcher=FakeResearcher(script), formalizer=FakeFormalizer(script))
    system.init_account(label="TEST")
    return Harness(system, clock, script, venue, ledger, settings)


@pytest.fixture
def harness(tmp_path) -> Harness:
    return build_harness(tmp_path)
