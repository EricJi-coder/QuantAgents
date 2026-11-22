"""TradingAgents → QuantDinger adapter: validated decisions in, ledger-booked paper fills out.

Order lifecycle (each transition is persisted before the next side effect):

    risk check ──fail──► REJECTED (reasons stored)
        │pass
    PENDING_SUBMIT (intent + idempotency key in the ledger)
        │ venue.submit(Idempotency-Key)
        ├─ filled ───► fill model on the venue's reference price ─► FILLED / PARTIALLY_FILLED
        ├─ rejected ─► FAILED (venue reason)
        └─ error ────► stays PENDING_SUBMIT; ``recover()`` resubmits with the same key

The idempotency key is derived from (run, ticker, side), so a retried job maps onto the same
ledger row and the same venue order and cannot double-fill.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from aipt.clock import Clock
from aipt.config import Settings
from aipt.execution.fill_model import FillModel
from aipt.execution.venues import PaperVenue, RealTradingRefused, VenueError
from aipt.ledger import DRAWDOWN_HALT, KILL_SWITCH, Ledger
from aipt.market.calendar import USEquityCalendar
from aipt.market.data import MarketData
from aipt.market.universe import check_instrument
from aipt.models import (
    AccountSnapshot,
    Fill,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
    TradeDecision,
)
from aipt.portfolio.construction import build_intents
from aipt.risk import RiskContext, RiskEngine

logger = logging.getLogger(__name__)


def idempotency_key(account_id: str, run_id: str, ticker: str, side: OrderSide) -> str:
    digest = hashlib.sha256(f"{account_id}|{run_id}|{ticker}|{side.value}".encode()).hexdigest()[:40]
    return f"aipt-{digest}"


@dataclass
class ExecutionReport:
    submitted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"submitted": self.submitted, "rejected": self.rejected, "failed": self.failed,
                "skipped": self.skipped}


class ExecutionAdapter:
    def __init__(self, settings: Settings, ledger: Ledger, venue: PaperVenue, market_data: MarketData,
                 clock: Clock, calendar: USEquityCalendar | None = None):
        if settings.mode != "paper":
            raise RealTradingRefused("execution adapter only runs in paper mode")
        self.settings = settings
        self.ledger = ledger
        self.venue = venue
        self.md = market_data
        self.clock = clock
        self.calendar = calendar or USEquityCalendar()
        self.risk = RiskEngine(settings.risk)
        self.fill_model = FillModel(settings.execution, fractional=settings.risk.allow_fractional_shares)

    # ----------------------------------------------------------------- state

    def snapshot(self, extra_tickers: list[str] | None = None) -> tuple[AccountSnapshot, dict[str, str]]:
        """Cash, positions and fresh quotes. A held position without a quote is fatal (equity unknown)."""
        positions = self.ledger.positions()
        prices: dict[str, Quote] = {}
        missing: dict[str, str] = {}
        for ticker in positions:
            prices[ticker] = self.md.quote(ticker)
        for ticker in extra_tickers or []:
            if ticker in prices:
                continue
            try:
                prices[ticker] = self.md.quote(ticker)
            except Exception as exc:
                missing[ticker] = str(exc)
        snap = AccountSnapshot(account_id=self.ledger.account_id, cash=self.ledger.cash(), positions=positions,
                               prices=prices, timestamp=self.clock.now())
        return snap, missing

    def session_start_equity(self, session_date: date, current_equity: float) -> float:
        same_day = [s for s in self.ledger.snapshots() if s["session_date"] == session_date.isoformat()]
        if same_day:
            return float(same_day[0]["equity"])
        closes = self.ledger.snapshots(kind="close")
        return float(closes[-1]["equity"]) if closes else current_equity

    # --------------------------------------------------------------- execute

    def execute(self, decisions: list[TradeDecision], run_id: str) -> ExecutionReport:
        report = ExecutionReport()
        now = self.clock.now()
        session_date = self.calendar.session_for(now).session_date
        tickers = [d.ticker for d in decisions]
        snap, missing = self.snapshot(tickers)
        adv = {t: self._adv(t) for t in tickers if t in snap.prices}
        intents = build_intents(decisions, snap, self.fill_model, adv)

        for d in decisions:
            if d.ticker in missing and d.target_weight is not None and d.action.value != "hold":
                report.failed.append({"ticker": d.ticker, "reason": f"no quote: {missing[d.ticker]}"})

        window_ok, window_reason = self.calendar.within_trading_window(
            now, self.settings.risk.session_open_buffer_minutes, self.settings.risk.session_close_buffer_minutes)
        pro_forma = snap.model_copy(deep=True)
        start_equity = self.session_start_equity(session_date, snap.equity)

        for intent in intents:
            if self.ledger.control(KILL_SWITCH).get("active"):
                report.skipped.append({"ticker": intent.ticker, "reason": "kill switch activated mid-batch"})
                break
            key = idempotency_key(self.ledger.account_id, run_id, intent.ticker, intent.side)
            existing = self._order_by_key(key)
            if existing and existing["status"] != OrderStatus.PENDING_SUBMIT.value:
                report.skipped.append({"ticker": intent.ticker, "side": intent.side.value,
                                       "order_id": existing["order_id"],
                                       "reason": f"already processed in this run ({existing['status']})"})
                continue
            if existing is None:
                halt = self.ledger.control(DRAWDOWN_HALT)
                ctx = RiskContext(
                    now=now, mode=self.ledger.account()["mode"], snapshot=pro_forma, session_date=session_date,
                    trading_window_ok=window_ok, trading_window_reason=window_reason,
                    kill_switch=bool(self.ledger.control(KILL_SWITCH).get("active")),
                    drawdown_halt=bool(halt.get("active")), drawdown=float(halt.get("drawdown") or 0.0),
                    session_start_equity=start_equity,
                    session_buy_notional=self.ledger.session_buy_notional(session_date),
                    session_orders_same_side=self.ledger.session_order_count(session_date, intent.ticker,
                                                                             intent.side),
                    instrument_reasons=self._instrument_reasons(intent.ticker))
                result = self.risk.evaluate(intent, ctx)
                if not result.approved:
                    order = self.ledger.record_rejection(idempotency_key=key, intent=intent,
                                                         session_date=session_date, run_id=run_id,
                                                         violations=result.violations, now=now)
                    report.rejected.append({"ticker": intent.ticker, "side": intent.side.value,
                                            "quantity": intent.quantity, "order_id": order["order_id"],
                                            "violations": [v.model_dump() for v in result.violations]})
                    continue
                order, _ = self.ledger.create_order(idempotency_key=key, intent=intent, session_date=session_date,
                                                    run_id=run_id, venue=self.venue.name, now=now)
            else:
                order = existing
            outcome = self._submit(order, intent, adv.get(intent.ticker))
            bucket = {"filled": report.submitted, "partially_filled": report.submitted}.get(outcome["status"],
                                                                                          report.failed)
            bucket.append(outcome)
            if outcome["status"] in ("filled", "partially_filled"):
                self._refresh_pro_forma(pro_forma)
            elif outcome["status"] == OrderStatus.PENDING_SUBMIT.value:
                report.failed.append({"ticker": intent.ticker, "reason": "venue unavailable; batch stopped"})
                break
        return report

    # ---------------------------------------------------------------- venue

    def _submit(self, order: dict[str, Any], intent: OrderIntent | None, adv: float | None) -> dict[str, Any]:
        ticker, side, qty = order["ticker"], OrderSide(order["side"]), float(order["quantity"])
        base = {"order_id": order["order_id"], "ticker": ticker, "side": side.value, "quantity": qty}
        try:
            result = self.venue.submit(idempotency_key=order["idempotency_key"], ticker=ticker, side=side,
                                       quantity=qty)
        except RealTradingRefused:
            self.ledger.set_control(KILL_SWITCH, {"active": True}, actor="system",
                                    note="venue indicated a non-paper execution path")
            raise
        except VenueError as exc:
            logger.error("venue error for %s: %s", order["order_id"], exc)
            self.ledger.audit("venue_error", {"order_id": order["order_id"], "error": str(exc)})
            return {**base, "status": OrderStatus.PENDING_SUBMIT.value, "error": str(exc)}

        now = self.clock.now()
        if result.status != "filled" or result.reference_price is None:
            self.ledger.update_order(order["order_id"], status=OrderStatus.FAILED, now=now,
                                     venue_order_id=result.venue_order_id, venue_status=result.status,
                                     reason=f"venue {result.status}: {result.note}")
            return {**base, "status": OrderStatus.FAILED.value, "venue_status": result.status, "note": result.note}

        reference_quote = Quote.model_validate_json(order["reference_quote"])
        deviation = abs(result.reference_price / reference_quote.price - 1)
        if deviation > self.settings.execution.max_reference_price_deviation:
            reason = (f"venue reference {result.reference_price:.4f} deviates {deviation:.2%} from pre-trade quote "
                      f"{reference_quote.price:.4f}; not booked")
            self.ledger.update_order(order["order_id"], status=OrderStatus.FAILED, now=now,
                                     venue_order_id=result.venue_order_id, venue_status=result.status, reason=reason)
            return {**base, "status": OrderStatus.FAILED.value, "note": reason}

        est = self.fill_model.estimate(side, qty, result.reference_price, adv)
        fill_qty = min(qty, result.filled_quantity or qty)
        commission, fees = self.fill_model.fees(side, fill_qty, est.fill_price)
        fill = Fill(order_id=order["order_id"], ticker=ticker, side=side, quantity=fill_qty,
                    reference_price=result.reference_price, fill_price=est.fill_price, slippage_bps=est.slippage_bps,
                    commission=commission, fees=fees, notional=fill_qty * est.fill_price,
                    timestamp=max(result.timestamp, now) if not result.replayed else now,
                    venue=result.venue, venue_order_id=result.venue_order_id)
        partial = fill_qty + 1e-9 < float(order["requested_quantity"])
        status = OrderStatus.PARTIALLY_FILLED if partial else OrderStatus.FILLED
        self.ledger.apply_fill(fill, status, venue_status=result.status)
        if partial:
            self.ledger.update_order(order["order_id"], status=status, now=now,
                                     reason=f"{order['reason'] or ''} | filled {fill_qty} of "
                                            f"{order['requested_quantity']} (ADV participation cap); remainder cancelled")
        return {**base, "status": status.value, "fill_price": fill.fill_price, "reference_price": fill.reference_price,
                "slippage_bps": fill.slippage_bps, "commission": commission, "fees": fees,
                "venue_order_id": result.venue_order_id, "replayed": result.replayed,
                "adv_known": est.adv_known}

    def recover(self) -> list[dict[str, Any]]:
        """Resolve orders left PENDING_SUBMIT by a crash or a venue outage.

        Inside the same session's trading window the order is resubmitted with its original
        idempotency key: the venue returns the original fill if it received the order, or
        fills it now if it did not. Outside that window a never-acknowledged order is
        cancelled rather than filled at a stale price.
        """
        outcomes = []
        now = self.clock.now()
        session = self.calendar.session_for(now)
        window_ok, window_reason = self.calendar.within_trading_window(
            now, self.settings.risk.session_open_buffer_minutes, self.settings.risk.session_close_buffer_minutes)
        for order in self.ledger.orders(status=[OrderStatus.PENDING_SUBMIT]):
            lookup = getattr(self.venue, "lookup", None)
            same_session = order["session_date"] == session.session_date.isoformat()
            if (window_ok and same_session) or (lookup is not None and lookup(order["idempotency_key"])):
                outcome = self._submit(order, None, self._adv(order["ticker"]))
            else:
                self.ledger.update_order(order["order_id"], status=OrderStatus.CANCELLED, now=now,
                                         reason=f"not acknowledged before the session window ended "
                                                f"({window_reason}); cancelled, never resubmitted at a stale price")
                outcome = {"order_id": order["order_id"], "ticker": order["ticker"], "status": "cancelled"}
            outcomes.append(outcome)
            self.ledger.audit("order_recovered", outcome)
        return outcomes

    # --------------------------------------------------------------- helpers

    def _order_by_key(self, key: str) -> dict[str, Any] | None:
        row = self.ledger._one("SELECT * FROM orders WHERE account_id=? AND idempotency_key=?",
                               (self.ledger.account_id, key))
        return dict(row) if row else None

    def _adv(self, ticker: str) -> float | None:
        try:
            return self.md.adv_shares(ticker)
        except Exception as exc:
            logger.warning("ADV unavailable for %s: %s", ticker, exc)
            return None

    def _instrument_reasons(self, ticker: str) -> list[str]:
        try:
            info = self.md.instrument_info(ticker)
        except Exception as exc:
            return [f"instrument metadata unavailable for {ticker}: {exc}"]
        return check_instrument(ticker, info, self.settings.risk)

    def _refresh_pro_forma(self, pro_forma: AccountSnapshot) -> None:
        pro_forma.cash = self.ledger.cash()
        pro_forma.positions = self.ledger.positions()


def fill_reference_for_replay(md, clock: Clock, latency_seconds: int):
    """Reference-price function for LocalPaperVenue in replays/backtests: next traded price after latency."""

    def reference(ticker: str, side: OrderSide) -> Quote:
        return md.next_open(ticker, clock.now() + timedelta(seconds=latency_seconds))

    return reference


def fill_reference_live(md):
    """Reference-price function for LocalPaperVenue in live use: a fresh quote at submission."""

    def reference(ticker: str, side: OrderSide) -> Quote:
        return md.quote(ticker)

    return reference


__all__ = ["ExecutionAdapter", "ExecutionReport", "Position", "idempotency_key", "fill_reference_for_replay",
           "fill_reference_live"]
