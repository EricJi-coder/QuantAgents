"""The real TradingAgents graph (analysts → debate → research manager → trader → risk debate →
portfolio manager) driven by the scripted, non-AI chat model, end to end into the ledger."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aipt.clock import SimulatedClock
from aipt.config import load_settings
from aipt.models import DecisionStatus
from aipt.research.scripted import LABEL
from aipt.system import build_replay
from conftest import make_store


def test_scripted_tradingagents_cycle_books_fills_and_reconciles(tmp_path):
    settings = load_settings(overrides={
        "paths": {"data_dir": str(tmp_path / "data"), "reports_dir": str(tmp_path / "reports")},
        "research": {"watchlist": ["AAA", "BBB"], "max_tickers_per_cycle": 2}, "execution": {"venue": "local"}})
    clock = SimulatedClock(datetime(2026, 11, 20, 10, 30, tzinfo=ZoneInfo("America/New_York")))
    system = build_replay(settings, store=make_store(daily_drift=0.004), clock=clock, research="scripted")
    system.init_account(label="TEST")

    result = system.run_cycle("1030", tickers=["AAA", "BBB"])

    assert result.status == "completed", result.detail
    decisions = system.ledger.decisions(run_id=result.run_id)
    assert {d.ticker for d in decisions} == {"AAA", "BBB"}
    for d in decisions:
        # A 0.4%/day uptrend is rated Overweight with confidence above the buy threshold.
        assert d.status is DecisionStatus.ACCEPTED and d.rating.value == "Overweight", d.rejection_reasons
        assert LABEL in d.thesis
    assert len(system.ledger.fills()) == 2
    assert result.detail["reconciliation"]["ok"]
