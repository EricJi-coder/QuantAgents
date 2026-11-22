"""End-to-end paper-trading scenarios on the offline harness (see conftest.py).

Each test drives TradingSystem.run_cycle the way the scheduler does, with scripted research
standing in for the LLMs, and asserts on the ledger, the venue and the published files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aipt.ledger import DRAWDOWN_HALT
from aipt.models import DecisionStatus, OrderStatus
from conftest import SESSION, build_harness, proposal

INITIAL = 100_000.00


def _orders(h, status=None):
    return h.ledger.orders(status=[status] if status else None)


def _codes(order) -> set[str]:
    return {v["code"] for v in json.loads(order["violations"] or "[]")}


# 1 ---------------------------------------------------------------------------------------

def test_account_starts_with_exactly_100k_cash(harness):
    acct = harness.ledger.account()
    assert acct["mode"] == "paper"
    assert acct["initial_capital"] == INITIAL
    assert harness.ledger.cash() == INITIAL
    assert harness.ledger.positions() == {}
    snap, _ = harness.system.adapter.snapshot()
    assert snap.equity == INITIAL
    # init is idempotent and cannot silently change the starting capital
    assert harness.system.init_account(label="TEST") is False
    assert harness.ledger.cash() == INITIAL


# 2-6 -------------------------------------------------------------------------------------

def test_full_cycle_buy_updates_cash_positions_and_equity(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA", target=0.05))
    result = harness.system.run_cycle("1030", tickers=["AAA"])
    assert result.status == "completed", result.detail

    # 2. a valid recommendation was accepted, with the machine-readable fields populated
    [d] = harness.ledger.decisions(run_id=result.run_id)
    assert d.status is DecisionStatus.ACCEPTED and d.action.value == "buy" and d.target_weight == 0.05
    assert d.decision_id.startswith("dec_") and d.evidence and d.confidence == 0.7 and d.risks
    assert d.data_timestamps["analysis_date"] == SESSION.isoformat() and "decision_quote_at" in d.data_timestamps

    # 3-4. it passed risk and became exactly one filled paper order
    [order] = _orders(harness)
    assert order["status"] == OrderStatus.FILLED.value and order["decision_id"] == d.decision_id
    assert len(harness.venue.list_orders()) == 1

    # 5. cash and position moved by the fill, including slippage, not the decision-time price
    [fill] = harness.ledger.fills()
    assert fill["fill_price"] > fill["reference_price"] == 100.0
    pos = harness.ledger.positions()["AAA"]
    assert pos.quantity == fill["quantity"] == 49  # floor(5000 / (100 * (1 + slippage)))
    spent = fill["quantity"] * fill["fill_price"] + fill["commission"] + fill["fees"]
    assert harness.ledger.cash() == pytest.approx(INITIAL - spent)

    # 6. equity = cash + marked positions, and the ledger identity reconciles with the venue
    snap, _ = harness.system.adapter.snapshot()
    assert snap.equity == pytest.approx(harness.ledger.cash() + 49 * 100.0)
    assert snap.equity == pytest.approx(INITIAL - 49 * (fill["fill_price"] - 100.0), abs=1e-6)
    rec = result.detail["reconciliation"]
    assert rec["ok"], rec["issues"]
    assert rec["venue_checked"] and rec["fills_checked"] == 1


def test_sell_closes_position_and_books_realized_pnl_with_fees(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA", target=0.05))
    assert harness.system.run_cycle("1030", tickers=["AAA"]).status == "completed"
    harness.at("14:00")
    harness.script.entries["AAA"] = ("Sell", proposal("AAA", action="sell", target=0.0))
    result = harness.system.run_cycle("1400", tickers=["AAA"])
    assert result.status == "completed", result.detail
    assert harness.ledger.positions() == {}
    sell = [f for f in harness.ledger.fills() if f["side"] == "sell"][0]
    assert sell["fees"] > 0  # SEC fee + FINRA TAF on sales
    assert harness.ledger.total_realized() < 0  # round trip at a flat price loses the costs
    assert result.detail["reconciliation"]["ok"]


# 7 ---------------------------------------------------------------------------------------

def test_oversized_position_is_rejected_even_with_unanimous_agents(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA", target=0.25, confidence=0.99))
    result = harness.system.run_cycle("1030", tickers=["AAA"])
    assert result.status == "completed"
    [order] = _orders(harness)
    assert order["status"] == OrderStatus.REJECTED.value
    assert "POSITION_LIMIT" in _codes(order)
    assert harness.venue.list_orders() == []  # never reached the venue
    assert harness.ledger.cash() == INITIAL and harness.ledger.positions() == {}


def test_daily_buy_turnover_cap_blocks_the_order_that_crosses_it(tmp_path):
    h = build_harness(tmp_path, {"research": {"max_tickers_per_cycle": 5}})
    for t in ("AAA", "BBB", "CCC", "SPY", "THIN"):
        h.script.entries[t] = ("Buy", proposal(t, target=0.08))
    h.system.run_cycle("1030", tickers=["AAA", "BBB", "CCC"])
    statuses = {o["ticker"]: o for o in _orders(h)}
    assert statuses["AAA"]["status"] == statuses["BBB"]["status"] == OrderStatus.FILLED.value
    assert statuses["CCC"]["status"] == OrderStatus.REJECTED.value  # 3 x 8% > 20% of equity
    assert "DAILY_TURNOVER" in _codes(statuses["CCC"])


# 8 ---------------------------------------------------------------------------------------

def test_retrying_a_completed_slot_does_not_duplicate_orders(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    first = harness.system.run_cycle("1030", tickers=["AAA"])
    again = harness.system.run_cycle("1030", tickers=["AAA"])
    assert first.status == "completed" and again.status == "already_completed"
    assert len(_orders(harness)) == 1 and len(harness.ledger.fills()) == 1
    assert len(harness.venue.list_orders()) == 1
    assert harness.script.calls == ["AAA"]  # research was not re-run either


def test_second_buy_same_ticker_same_session_is_rejected_as_duplicate(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA", target=0.05))
    harness.system.run_cycle("1030", tickers=["AAA"])
    harness.at("13:00")
    harness.script.entries["AAA"] = ("Buy", proposal("AAA", target=0.09))
    harness.system.run_cycle("1300", tickers=["AAA"])
    rejected = _orders(harness, OrderStatus.REJECTED)
    assert len(rejected) == 1 and "DUPLICATE_ORDER" in _codes(rejected[0])
    assert len(harness.ledger.fills()) == 1


def test_venue_resubmission_with_same_key_returns_original_fill(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    harness.system.run_cycle("1030", tickers=["AAA"])
    [order] = _orders(harness)
    from aipt.models import OrderSide
    replay = harness.venue.submit(idempotency_key=order["idempotency_key"], ticker="AAA", side=OrderSide.BUY,
                                  quantity=order["quantity"])
    assert replay.replayed and replay.venue_order_id == order["venue_order_id"]
    assert len(harness.venue.list_orders()) == 1


# 9 ---------------------------------------------------------------------------------------

def test_recovers_from_crash_between_venue_fill_and_ledger_booking(harness, monkeypatch):
    """The venue filled the order but the process died before the ledger booked it."""
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    real_apply = harness.ledger.apply_fill

    def crash(*a, **kw):
        raise SystemError("process killed mid-booking")

    monkeypatch.setattr(harness.ledger, "apply_fill", crash)
    crashed = harness.system.run_cycle("1030", tickers=["AAA"])
    assert crashed.status == "failed"
    [pending] = _orders(harness, OrderStatus.PENDING_SUBMIT)
    assert len(harness.venue.list_orders()) == 1 and harness.ledger.fills() == []

    monkeypatch.setattr(harness.ledger, "apply_fill", real_apply)
    harness.at("10:35")
    resumed = harness.system.run_cycle("1030", tickers=["AAA"])
    assert resumed.status == "completed", resumed.detail
    assert resumed.detail["recovered"][0]["status"] == "filled"
    assert harness.script.calls == ["AAA"]  # decision reused, not re-researched
    assert len(harness.venue.list_orders()) == 1  # same idempotency key: no second venue order
    assert len(harness.ledger.fills()) == 1
    assert harness.ledger.order(pending["order_id"])["status"] == OrderStatus.FILLED.value
    assert resumed.detail["reconciliation"]["ok"]


def test_venue_outage_leaves_order_pending_then_retry_fills_it(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    harness.venue.fail_next = "error"
    first = harness.system.run_cycle("1030", tickers=["AAA"])
    assert first.status == "incomplete"
    assert len(_orders(harness, OrderStatus.PENDING_SUBMIT)) == 1
    retry = harness.system.run_cycle("1030", tickers=["AAA"])
    assert retry.status == "completed"
    assert len(harness.ledger.fills()) == 1 and len(harness.venue.list_orders()) == 1


def test_pending_order_after_session_is_cancelled_not_filled_stale(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    harness.venue.fail_next = "error"
    harness.system.run_cycle("1030", tickers=["AAA"])
    harness.at("17:00")
    outcomes = harness.system.adapter.recover()
    assert outcomes[0]["status"] == "cancelled"
    assert harness.ledger.fills() == [] and harness.venue.list_orders() == []


# 10 --------------------------------------------------------------------------------------

def test_produces_savvytrader_publication_record_and_tracks_discrepancy(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    result = harness.system.run_cycle("1030", tickers=["AAA"])
    files = result.detail["publication"]["files"]
    record = json.loads(Path(files["json"]).read_text())
    assert record["simulated"] is True and record["ai_managed"] is True
    assert "SIMULATED" in record["disclaimer"] and "SIMULATED" in record["portfolio_name"]
    assert record["account"]["initial_capital"] == INITIAL
    assert [h["ticker"] for h in record["holdings"]] == ["AAA"]
    [entry] = record["trade_history"]
    assert entry["savvytrader_status"] == "pending" and entry["savvytrader_price"] is None
    assert entry["comment"].startswith("[SIMULATED AI PAPER PORTFOLIO]")
    csv_text = Path(files["queue_csv"]).read_text()
    assert csv_text.splitlines()[1].split(",")[2:5] == ["BUY", "AAA", "49"]

    # operator enters the trade in SavvyTrader and records SavvyTrader's own virtual price
    pub = harness.system.publisher
    pub.confirm(entry["entry_id"], savvy_price=100.50, savvy_quantity=49, savvy_ts=None, entered_by="op")
    [disc] = pub.discrepancies()
    assert disc["savvy_price"] == 100.50 and disc["price_diff_bps"] > 0
    assert disc["local_price"] == harness.ledger.fills()[0]["fill_price"]  # both records kept
    assert pub.sync_queue() == 0  # re-sync does not duplicate entries


# Risk policy and safety ------------------------------------------------------------------

def test_no_orders_outside_regular_hours(harness):
    harness.at("09:32")  # inside the 5-minute opening buffer
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    harness.system.run_cycle("0932", tickers=["AAA"])
    [order] = _orders(harness)
    assert order["status"] == OrderStatus.REJECTED.value and "MARKET_CLOSED" in _codes(order)


def test_leveraged_etf_outside_universe_is_rejected(harness):
    harness.script.entries["TQQQ"] = ("Buy", proposal("TQQQ"))
    harness.system.run_cycle("1030", tickers=["TQQQ"])
    [order] = _orders(harness)
    assert "UNIVERSE" in _codes(order) and harness.venue.list_orders() == []


def test_partial_fill_when_order_exceeds_adv_participation(harness):
    harness.script.entries["THIN"] = ("Buy", proposal("THIN", target=0.05))
    harness.system.run_cycle("1030", tickers=["THIN"])
    [order] = _orders(harness)
    assert order["status"] == OrderStatus.PARTIALLY_FILLED.value
    assert order["quantity"] == 10 and order["requested_quantity"] == 249  # 1% of 1,000 ADV
    assert harness.ledger.positions()["THIN"].quantity == 10


def test_drawdown_halt_blocks_buys_but_allows_sells(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    harness.system.run_cycle("1030", tickers=["AAA"])
    # A prior running peak 20% above current equity.
    harness.ledger.record_snapshot(ts=harness.clock.now(), session_date=SESSION, cash=125_000, market_value=0,
                                   prices={}, kind="intraday")
    harness.at("11:00")
    harness.system.mark_to_market()
    assert harness.ledger.control(DRAWDOWN_HALT)["active"] is True

    harness.script.entries["BBB"] = ("Buy", proposal("BBB"))
    harness.script.entries["AAA"] = ("Sell", proposal("AAA", action="sell", target=0.0))
    harness.system.run_cycle("1100", tickers=["AAA", "BBB"])
    by_ticker = {o["ticker"]: o for o in _orders(harness) if o["run_id"].endswith(":1100")}
    assert "DRAWDOWN_HALT" in _codes(by_ticker["BBB"])
    assert by_ticker["AAA"]["status"] == OrderStatus.FILLED.value

    with pytest.raises(ValueError):
        harness.system.clear_drawdown_halt("reviewer", "  ")
    harness.system.clear_drawdown_halt("reviewer", "reviewed: test peak was synthetic")
    assert harness.ledger.control(DRAWDOWN_HALT)["active"] is False


def test_kill_switch_stops_the_cycle_before_research(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    harness.system.kill("operator", "test")
    result = harness.system.run_cycle("1030", tickers=["AAA"])
    assert result.status == "halted" and harness.script.calls == [] and _orders(harness) == []
    harness.system.resume("operator", "test over")
    assert harness.system.run_cycle("1031", tickers=["AAA"]).status == "completed"


def test_cash_reserve_is_kept(tmp_path):
    h = build_harness(tmp_path, {"risk": {"max_position_weight": 1.0, "max_daily_buy_turnover": 1.0}})
    h.script.entries["AAA"] = ("Buy", proposal("AAA", target=0.97))
    h.system.run_cycle("1030", tickers=["AAA"])
    [order] = _orders(h)
    assert "CASH_RESERVE" in _codes(order)


def test_close_mark_credits_dividends_only_for_shares_held_before_ex_date(tmp_path):
    from datetime import date

    import pandas as pd

    from conftest import make_store

    store = make_store()
    store.dividends_by_ticker["AAA"] = pd.Series([0.50], index=pd.DatetimeIndex([pd.Timestamp("2026-11-23")]))
    h = build_harness(tmp_path, store=store)
    h.script.entries["AAA"] = ("Buy", proposal("AAA"))
    h.script.entries["BBB"] = ("Buy", proposal("BBB"))
    h.system.run_cycle("1030", tickers=["AAA", "BBB"])  # BBB has no dividend history at all
    cash_before = h.ledger.cash()
    h.at("16:10", day=date(2026, 11, 23))
    h.system.mark_to_market("close")
    h.system.mark_to_market("close")  # idempotent
    assert h.ledger.total_dividends() == pytest.approx(0.50 * 49)
    assert h.ledger.cash() == pytest.approx(cash_before + 0.50 * 49)
    assert h.system.reconcile()["ok"]


# Decision validation ---------------------------------------------------------------------

@pytest.mark.parametrize("rating, prop, reason", [
    ("Buy", None, "formalizer produced no structured decision"),
    ("Sell", proposal("AAA", action="buy"), "contradicts"),
    ("Buy", proposal("AAA", fact=False), "verified fact"),
    ("Buy", proposal("AAA", as_of="2026-11-21"), "look-ahead"),
    ("Buy", proposal("AAA", ticker_override="BBB"), "not AAA"),
    ("REVIEW", proposal("AAA"), "no parseable rating"),
])
def test_invalid_llm_output_is_rejected_and_never_executed(harness, rating, prop, reason):
    harness.script.entries["AAA"] = (rating, prop)
    result = harness.system.run_cycle("1030", tickers=["AAA"])
    [d] = harness.ledger.decisions(run_id=result.run_id)
    assert d.status is DecisionStatus.REJECTED and d.target_weight is None
    assert any(reason in r for r in d.rejection_reasons), d.rejection_reasons
    assert _orders(harness) == []


def test_low_confidence_buy_becomes_hold(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA", confidence=0.4))
    result = harness.system.run_cycle("1030", tickers=["AAA"])
    [d] = harness.ledger.decisions(run_id=result.run_id)
    assert d.status is DecisionStatus.ACCEPTED and d.action.value == "hold"
    assert _orders(harness) == []


def test_research_failure_for_one_ticker_does_not_stop_others(harness):
    harness.script.entries["AAA"] = ("Buy", proposal("AAA"))
    harness.script.entries["BBB"] = ("Buy", proposal("BBB"))
    harness.script.fail_on.add("AAA")
    result = harness.system.run_cycle("1030", tickers=["AAA", "BBB"])
    assert result.status == "completed"
    assert [o["ticker"] for o in _orders(harness)] == ["BBB"]
