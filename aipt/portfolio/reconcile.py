"""Reconciliation: ledger internal consistency, and ledger fills vs the venue's order records."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from aipt.ledger import Ledger
from aipt.models import Quote

TOL = 1e-6
# QuantDinger's GET /portfolio/paper-orders returns only the newest 200 records.
VENUE_LIST_CAP = 200


def reconcile(ledger: Ledger, venue_orders: list[dict[str, Any]] | None, prices: dict[str, Quote],
              venue_list_cap: int | None = VENUE_LIST_CAP) -> dict[str, Any]:
    issues: list[str] = []
    fills = ledger.fills()
    account = ledger.account()

    # 1. Positions equal the sum of fills.
    qty_from_fills: dict[str, float] = defaultdict(float)
    for f in fills:
        qty_from_fills[f["ticker"]] += f["quantity"] if f["side"] == "buy" else -f["quantity"]
    positions = ledger.positions(include_flat=True)
    for ticker in set(qty_from_fills) | set(positions):
        held = positions[ticker].quantity if ticker in positions else 0.0
        if abs(held - qty_from_fills.get(ticker, 0.0)) > TOL:
            issues.append(f"{ticker}: position {held} != sum of fills {qty_from_fills.get(ticker, 0.0)}")
        if held < -TOL:
            issues.append(f"{ticker}: negative position {held} (short)")

    # 2. Cash equals initial capital + trade cash flows + dividends, and is never negative.
    cash = ledger.cash()
    trade_cash = sum(-(f["notional"] + f["commission"] + f["fees"]) if f["side"] == "buy"
                     else f["notional"] - f["commission"] - f["fees"] for f in fills)
    expected_cash = account["initial_capital"] + trade_cash + ledger.total_dividends()
    if abs(cash - expected_cash) > 1e-4:
        issues.append(f"cash {cash:.4f} != initial + trades + dividends {expected_cash:.4f}")
    if cash < -TOL:
        issues.append(f"negative cash {cash:.2f}")

    # 3. Equity identity: equity == initial + dividends + realized + unrealized.
    open_positions = ledger.positions()
    unrealized = 0.0
    market_value = 0.0
    for ticker, p in open_positions.items():
        if ticker not in prices:
            issues.append(f"{ticker}: no price to mark")
            continue
        market_value += p.quantity * prices[ticker].price
        unrealized += p.quantity * (prices[ticker].price - p.average_cost)
    equity = cash + market_value
    identity = account["initial_capital"] + ledger.total_dividends() + ledger.total_realized() + unrealized
    if abs(equity - identity) > 1e-4:
        issues.append(f"equity {equity:.4f} != initial + dividends + realized + unrealized {identity:.4f}")

    # 4. Ledger fills vs venue records.
    venue_checked = venue_orders is not None
    unchecked_old_fills = 0
    if venue_checked:
        by_uid = {o.get("order_uid"): o for o in venue_orders}
        truncated = venue_list_cap is not None and len(venue_orders) >= venue_list_cap
        for f in fills:
            v = by_uid.get(f["venue_order_id"])
            if v is None:
                if truncated:
                    unchecked_old_fills += 1
                    continue
                issues.append(f"fill {f['fill_id']} ({f['ticker']}) has no venue record {f['venue_order_id']}")
                continue
            if str(v.get("symbol")).upper() != f["ticker"] or str(v.get("side")).lower() != f["side"]:
                issues.append(f"fill {f['fill_id']}: venue symbol/side {v.get('symbol')}/{v.get('side')} differ")
            if abs(float(v.get("qty") or 0) - f["quantity"]) > TOL:
                issues.append(f"fill {f['fill_id']}: venue qty {v.get('qty')} != ledger {f['quantity']}")
            if v.get("fill_price") is None or abs(float(v["fill_price"]) - f["reference_price"]) > 1e-6:
                issues.append(f"fill {f['fill_id']}: venue price {v.get('fill_price')} != reference "
                              f"{f['reference_price']}")
        booked = {f["venue_order_id"] for f in fills}
        refused = {o["venue_order_id"] for o in ledger.orders() if o["venue_order_id"] and o["status"] == "failed"}
        for uid, v in by_uid.items():
            if v.get("status") == "filled" and uid not in booked:
                kind = "refused by the ledger (see order reason)" if uid in refused else "unknown to the ledger"
                issues.append(f"venue order {uid} ({v.get('symbol')} {v.get('side')} {v.get('qty')}) is filled "
                              f"at the venue but {kind}")

    return {
        "ok": not issues, "issues": issues, "cash": cash, "market_value": market_value, "equity": equity,
        "realized_pnl": ledger.total_realized(), "unrealized_pnl": unrealized, "dividends": ledger.total_dividends(),
        "fills_checked": len(fills), "venue_checked": venue_checked,
        "fills_older_than_venue_list": unchecked_old_fills,
    }
