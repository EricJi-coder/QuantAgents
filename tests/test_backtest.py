"""Chronological backtest on synthetic daily bars (no network)."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

from aipt.backtest import run_backtest
from aipt.config import load_settings
from aipt.ledger import Ledger
from conftest import make_store


def test_backtest_fills_at_each_sessions_open_and_is_labelled(tmp_path):
    store = make_store(daily_drift=0.004)
    for key in [k for k in store.bars if k[1] == "1m"]:
        del store.bars[key]  # a daily-bar backtest has no intraday data
    settings = load_settings(overrides={
        "paths": {"data_dir": str(tmp_path / "data"), "reports_dir": str(tmp_path / "reports")},
        "research": {"watchlist": ["AAA", "BBB"]}, "execution": {"venue": "local"}})

    out = run_backtest(settings, date(2026, 11, 16), date(2026, 11, 20), name="t", tickers=["AAA", "BBB"],
                       store=store)

    assert out["label"] == "HISTORICAL_BACKTEST"
    assert any(f.startswith("SURVIVORSHIP") for f in out["flags"])
    assert any(f.startswith("SCRIPTED") for f in out["flags"])
    assert [s["status"] for s in out["sessions"]] == ["completed"] * 5

    bt = Ledger(tmp_path / "data" / "backtests" / "t" / "ledger.db", f"{settings.account.id}-bt-t")
    assert bt.account()["label"] == "HISTORICAL_BACKTEST"
    fills = bt.fills()
    assert fills
    orders = {o["order_id"]: o for o in bt.orders()}
    for f in fills:
        session = orders[f["order_id"]]["session_date"]
        assert f["ts"][:10] == session  # booked on the session it was ordered, not the next one
        day_open = store.bars[(f["ticker"], "1d")].loc[pd.Timestamp(session, tz="UTC"), "open"]
        assert f["reference_price"] == day_open
    assert all(o["status"] != "failed" for o in orders.values())

    result = json.loads((tmp_path / "reports" / "backtests" / "t" / "backtest_result.json").read_text())
    assert result["performance"]["equity"] > 0
