"""Transaction-cost and fill assumptions (documented in docs/ARCHITECTURE.md).

- Market orders only, regular session only.
- The fill reference is a price observed at submission time (the venue's own reference for
  QuantDinger; the next traded price after submission + latency in replays/backtests), never
  the price the research saw at decision time.
- Slippage = half-spread + linear impact in participation of 20-day ADV, capped; adverse to
  the side (buys pay up, sells receive less).
- Orders are capped at ``max_participation_of_adv`` of ADV; the excess is not filled and the
  order is recorded as partially filled. With ADV unknown, the order fills in full and the
  fill is flagged.
- Commission per share (default $0, typical US retail), SEC Section 31 fee on sale proceeds,
  FINRA TAF per share sold (capped).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aipt.config import ExecutionConfig
from aipt.models import OrderSide


@dataclass(frozen=True)
class FillEstimate:
    fillable_quantity: float
    participation: float | None
    slippage_bps: float
    fill_price: float
    commission: float
    fees: float
    notional: float
    adv_known: bool

    @property
    def costs(self) -> float:
        return self.commission + self.fees


class FillModel:
    def __init__(self, cfg: ExecutionConfig, fractional: bool = False):
        self.cfg = cfg
        self.fractional = fractional

    def round_quantity(self, qty: float) -> float:
        if qty <= 0:
            return 0.0
        return round(qty, 6) if self.fractional else float(math.floor(qty + 1e-9))

    def fillable(self, quantity: float, adv_shares: float | None) -> float:
        if not adv_shares or adv_shares <= 0:
            return quantity
        return min(quantity, self.round_quantity(adv_shares * self.cfg.max_participation_of_adv))

    def slippage_bps(self, quantity: float, adv_shares: float | None) -> tuple[float, float | None]:
        participation = quantity / adv_shares if adv_shares and adv_shares > 0 else None
        impact = self.cfg.impact_bps_per_pct_adv * (participation * 100.0) if participation is not None else 0.0
        return min(self.cfg.half_spread_bps + impact, self.cfg.max_slippage_bps), participation

    def fees(self, side: OrderSide, quantity: float, price: float) -> tuple[float, float]:
        commission = max(self.cfg.commission_per_share * quantity, self.cfg.commission_min if quantity > 0 else 0.0)
        fees = 0.0
        if side is OrderSide.SELL:
            fees += quantity * price * self.cfg.sec_fee_per_million / 1_000_000
            fees += min(quantity * self.cfg.finra_taf_per_share, self.cfg.finra_taf_max)
        return round(commission, 4), round(fees, 4)

    def estimate(self, side: OrderSide, quantity: float, reference_price: float,
                 adv_shares: float | None) -> FillEstimate:
        qty = self.fillable(quantity, adv_shares)
        slip, participation = self.slippage_bps(qty, adv_shares)
        sign = 1 if side is OrderSide.BUY else -1
        price = reference_price * (1 + sign * slip / 10_000)
        commission, fees = self.fees(side, qty, price)
        return FillEstimate(fillable_quantity=qty, participation=participation, slippage_bps=slip,
                            fill_price=price, commission=commission, fees=fees, notional=qty * price,
                            adv_known=bool(adv_shares))
