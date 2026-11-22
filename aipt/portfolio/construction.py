"""Target weights → order intents, sized at a fresh pre-trade quote."""

from __future__ import annotations

import math

from aipt.execution.fill_model import FillModel
from aipt.models import AccountSnapshot, Action, DecisionStatus, OrderIntent, OrderSide, TradeDecision


def build_intents(decisions: list[TradeDecision], snapshot: AccountSnapshot, fill_model: FillModel,
                  adv: dict[str, float | None]) -> list[OrderIntent]:
    """Sells first (they free cash), then buys, each in decision order."""
    sells, buys = [], []
    equity = snapshot.equity
    for d in decisions:
        if d.status is not DecisionStatus.ACCEPTED or d.action is Action.HOLD or d.target_weight is None:
            continue
        quote = snapshot.prices.get(d.ticker)
        if quote is None:
            continue
        held = snapshot.positions.get(d.ticker)
        held_qty = held.quantity if held else 0.0
        delta_value = d.target_weight * equity - held_qty * quote.price
        if d.action is Action.BUY and delta_value > 0:
            est = fill_model.estimate(OrderSide.BUY, 1.0, quote.price, None)
            requested = fill_model.round_quantity(delta_value / (est.fill_price + est.costs))
            side, bucket = OrderSide.BUY, buys
        elif d.action is Action.SELL and delta_value < 0:
            requested = held_qty if d.target_weight <= 0 else min(
                held_qty, fill_model.round_quantity(math.ceil(-delta_value / quote.price - 1e-9)))
            side, bucket = OrderSide.SELL, sells
        else:
            continue
        if requested <= 0:
            continue
        est = fill_model.estimate(side, requested, quote.price, adv.get(d.ticker))
        if est.fillable_quantity <= 0:
            continue
        bucket.append(OrderIntent(
            ticker=d.ticker, side=side, quantity=est.fillable_quantity, requested_quantity=requested,
            reference_quote=quote, estimated_fill_price=est.fill_price, estimated_notional=est.notional,
            estimated_costs=est.costs, decision_id=d.decision_id,
            reason=f"{d.rating.value}: target {d.target_weight:.2%} from {snapshot.weight(d.ticker):.2%}"))
    return sells + buys
