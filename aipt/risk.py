"""Deterministic pre-trade risk policy.

Runs after research and portfolio construction and before the venue. It reads only the
order intent and the account state — never the agents' reasoning — so unanimous agent
agreement cannot override it. Any violation blocks the order; nothing is resized here.

Checks are evaluated on a *pro-forma* snapshot that already includes earlier approved
orders from the same batch (sells first), so cash, exposure and turnover limits hold for
the batch as a whole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from aipt.config import RiskConfig
from aipt.models import AccountSnapshot, OrderIntent, OrderSide, RiskResult, RiskViolation


@dataclass
class RiskContext:
    now: datetime
    mode: str
    snapshot: AccountSnapshot               # pro forma, after earlier approved orders in this batch
    session_date: date
    trading_window_ok: bool
    trading_window_reason: str
    kill_switch: bool
    drawdown_halt: bool
    drawdown: float
    session_start_equity: float
    session_buy_notional: float             # filled + pending + earlier approved in this batch
    session_orders_same_side: int           # orders for this ticker+side already this session
    instrument_reasons: list[str] = field(default_factory=list)


class RiskEngine:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def evaluate(self, intent: OrderIntent, ctx: RiskContext) -> RiskResult:
        v: list[RiskViolation] = []
        checks: list[str] = []

        def check(name: str, ok: bool, code: str, message: str) -> None:
            checks.append(name)
            if not ok:
                v.append(RiskViolation(code=code, message=message))

        snap = ctx.snapshot
        equity = snap.equity
        cfg = self.cfg

        check("paper_mode", ctx.mode == "paper", "NOT_PAPER", f"account mode is {ctx.mode!r}; only paper is allowed")
        check("kill_switch", not ctx.kill_switch, "KILL_SWITCH", "kill switch is active")
        check("market_session", ctx.trading_window_ok, "MARKET_CLOSED",
              f"outside the permitted regular-session window: {ctx.trading_window_reason}")
        check("universe", not ctx.instrument_reasons, "UNIVERSE", "; ".join(ctx.instrument_reasons))
        check("positive_quantity", intent.quantity > 0, "BAD_QUANTITY", "quantity must be positive")
        if not cfg.allow_fractional_shares:
            check("whole_shares", float(intent.quantity).is_integer(), "FRACTIONAL",
                  f"fractional quantity {intent.quantity} is not allowed")
        age = intent.reference_quote.age_seconds(ctx.now)
        check("quote_fresh", 0 <= age <= cfg.max_quote_age_seconds, "STALE_QUOTE",
              f"quote for {intent.ticker} is {age:.0f}s old (limit {cfg.max_quote_age_seconds}s)")
        check("duplicate", ctx.session_orders_same_side < cfg.max_orders_per_ticker_per_session, "DUPLICATE_ORDER",
              f"{ctx.session_orders_same_side} {intent.side.value} order(s) for {intent.ticker} already this session")
        check("min_notional", intent.estimated_notional >= cfg.min_order_notional, "BELOW_MIN_NOTIONAL",
              f"order notional {intent.estimated_notional:,.2f} below minimum {cfg.min_order_notional:,.2f}")

        if intent.side is OrderSide.SELL:
            held = snap.positions.get(intent.ticker)
            held_qty = held.quantity if held else 0.0
            check("no_short", intent.quantity <= held_qty + 1e-9, "SHORT_SALE",
                  f"sell {intent.quantity} exceeds holding {held_qty}; short selling is not allowed")
            return RiskResult(approved=not v, violations=v, checks=checks)

        # --- risk-increasing (buy) checks -----------------------------------------------
        check("drawdown_halt", not ctx.drawdown_halt, "DRAWDOWN_HALT",
              f"drawdown halt active (drawdown {ctx.drawdown:.2%} >= {cfg.drawdown_halt:.0%}); "
              "new risk requires manual review")
        cost = intent.estimated_notional + intent.estimated_costs
        cash_after = snap.cash - cost
        # Equity after the buy falls only by costs; positions are marked at the reference quote.
        equity_after = equity - intent.estimated_costs - (intent.estimated_fill_price - intent.reference_quote.price) * intent.quantity
        held = snap.positions.get(intent.ticker)
        held_value = (held.quantity if held else 0.0) * intent.reference_quote.price
        position_after = held_value + intent.quantity * intent.reference_quote.price
        weight_after = position_after / equity_after if equity_after > 0 else float("inf")
        check("no_margin", cash_after >= 0, "INSUFFICIENT_CASH",
              f"needs {cost:,.2f} but only {snap.cash:,.2f} cash; margin and borrowing are not allowed")
        check("cash_reserve", cash_after >= cfg.min_cash_reserve * equity_after, "CASH_RESERVE",
              f"cash after trade {cash_after:,.2f} below {cfg.min_cash_reserve:.0%} reserve "
              f"({cfg.min_cash_reserve * equity_after:,.2f})")
        check("position_limit", weight_after <= cfg.max_position_weight + 1e-9, "POSITION_LIMIT",
              f"{intent.ticker} would be {weight_after:.2%} of equity (limit {cfg.max_position_weight:.0%})")
        gross_after = (snap.market_value + intent.quantity * intent.reference_quote.price) / equity_after \
            if equity_after > 0 else float("inf")
        check("gross_exposure", gross_after <= cfg.max_gross_exposure + 1e-9, "GROSS_EXPOSURE",
              f"gross long exposure would be {gross_after:.2%} (limit {cfg.max_gross_exposure:.0%})")
        turnover_after = ctx.session_buy_notional + intent.estimated_notional
        limit = cfg.max_daily_buy_turnover * ctx.session_start_equity
        check("daily_turnover", turnover_after <= limit + 1e-6, "DAILY_TURNOVER",
              f"purchases this session would total {turnover_after:,.2f} (limit {limit:,.2f}, "
              f"{cfg.max_daily_buy_turnover:.0%} of session-start equity)")
        return RiskResult(approved=not v, violations=v, checks=checks)
