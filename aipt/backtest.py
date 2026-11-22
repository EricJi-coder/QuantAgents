"""Chronological daily backtest through the same cycle, risk engine, adapter and ledger.

Per session D (oldest first):
  1. clock = D's open + 1 minute; research sees data through the previous session's close
     (``analysis_date`` = previous session), so no bar from D or later reaches a decision;
  2. orders are market-on-open: the reference price is D's official open, plus modelled
     slippage and fees (the only intraday price a daily bar reveals before the close);
  3. clock = D's close + 10 minutes; mark to market at D's close, credit dividends.

Results go to a separate ledger labelled HISTORICAL_BACKTEST and never mix with live-forward
paper results. Known limits are written into the result rather than left implicit:
- survivorship: the universe is today's watchlist, chosen with hindsight;
- LLM contamination: an LLM whose training data covers the period may "know" the outcome;
  without a configured knowledge cutoff after the period, results are flagged.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from aipt.clock import SimulatedClock
from aipt.config import Settings
from aipt.market.calendar import USEquityCalendar
from aipt.market.data import BarStore

logger = logging.getLogger(__name__)


def backtest_settings(settings: Settings, name: str) -> Settings:
    root = Path(settings.paths.data_dir) / "backtests" / name
    reports = Path(settings.paths.reports_dir) / "backtests" / name
    return settings.model_copy(update={
        "paths": settings.paths.model_copy(update={"data_dir": root, "reports_dir": reports}),
        "account": settings.account.model_copy(update={"id": f"{settings.account.id}-bt-{name}"}),
        # Market-on-open orders: no open buffer; the close buffer still applies.
        "risk": settings.risk.model_copy(update={"session_open_buffer_minutes": 0}),
    }, deep=True)


def contamination_flags(settings: Settings, research: str, end: date) -> list[str]:
    flags = ["SURVIVORSHIP: universe is the current watchlist, chosen with hindsight; delisted names are absent"]
    if research == "tradingagents":
        cutoff = settings.llm.knowledge_cutoff
        if not cutoff:
            flags.append("LLM_LOOKAHEAD_RISK: llm.knowledge_cutoff is not configured; the model may have been "
                         "trained on data from the backtest period")
        elif cutoff >= end.isoformat():
            flags.append(f"LLM_LOOKAHEAD_RISK: model knowledge cutoff {cutoff} is on/after the period end {end}")
    else:
        flags.append("SCRIPTED: decisions come from a deterministic price rule, not AI research")
    return flags


def run_backtest(settings: Settings, start: date, end: date, *, name: str, research: str = "scripted",
                 tickers: list[str] | None = None, store: BarStore | None = None, fresh: bool = True) -> dict[str, Any]:
    from aipt.system import build_replay

    calendar = USEquityCalendar()
    sessions = calendar.sessions_between(start, end)
    if not sessions:
        raise ValueError(f"no NYSE sessions between {start} and {end}")
    bt = backtest_settings(settings, name)
    if fresh:
        shutil.rmtree(bt.paths.data_dir, ignore_errors=True)
        shutil.rmtree(bt.paths.reports_dir, ignore_errors=True)
    universe = tickers or bt.research.watchlist
    if store is None:
        store = BarStore.from_yahoo(list(dict.fromkeys(universe + [bt.benchmark.ticker])),
                                    start - timedelta(days=120), end)
    clock = SimulatedClock(calendar.session_open(sessions[0]) - timedelta(hours=1))
    system = build_replay(bt, store=store, clock=clock, research=research, label_dir="backtest",
                          market_on_open=True)
    system.init_account(label="HISTORICAL_BACKTEST")

    results = []
    for day in sessions:
        clock.set(calendar.session_open(day) + timedelta(minutes=1))
        res = system.run_cycle("open", tickers=universe, analysis_date=calendar.previous_session(day))
        results.append({"session": day.isoformat(), "status": res.status, "error": res.detail.get("error")})
        clock.set(calendar.session_close(day) + timedelta(minutes=10))
        system.mark_to_market("close")
        if res.status == "failed":
            logger.error("backtest session %s failed: %s", day, res.detail.get("error"))

    perf = system.performance()
    out = {
        "label": "HISTORICAL_BACKTEST",
        "name": name, "start": sessions[0].isoformat(), "end": sessions[-1].isoformat(),
        "research": research, "universe": universe,
        "execution_model": "market-on-open at the official open + modelled slippage/fees; decisions use data "
                           "through the previous close",
        "flags": contamination_flags(settings, research, end),
        "sessions": results, "performance": perf,
        "llm_usage": system.ledger.llm_usage_summary(),
        "final_positions": {t: p.model_dump() for t, p in system.ledger.positions().items()},
        "final_cash": system.ledger.cash(),
    }
    reports = Path(bt.paths.reports_dir)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "backtest_result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out
