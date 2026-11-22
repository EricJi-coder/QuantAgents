"""Typed domain models shared by research, risk, execution, ledger and publishing."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class Rating(StrEnum):
    """TradingAgents' 5-tier Portfolio Manager rating, plus its non-tradeable REVIEW sentinel."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"
    REVIEW = "REVIEW"


RATING_DIRECTION: dict[Rating, Action] = {
    Rating.BUY: Action.BUY,
    Rating.OVERWEIGHT: Action.BUY,
    Rating.HOLD: Action.HOLD,
    Rating.UNDERWEIGHT: Action.SELL,
    Rating.SELL: Action.SELL,
}


class EvidenceKind(StrEnum):
    VERIFIED_FACT = "verified_fact"   # stated in an analyst report that was built from tool data
    HYPOTHESIS = "hypothesis"         # an inference, forecast or opinion


class EvidenceSource(StrEnum):
    MARKET_REPORT = "market_report"
    SENTIMENT_REPORT = "sentiment_report"
    NEWS_REPORT = "news_report"
    FUNDAMENTALS_REPORT = "fundamentals_report"
    INVESTMENT_DEBATE = "investment_debate"
    RISK_DEBATE = "risk_debate"
    PORTFOLIO_MANAGER = "portfolio_manager"


class Evidence(BaseModel):
    statement: str = Field(min_length=3, description="One specific claim, quoted or closely paraphrased from the source.")
    kind: EvidenceKind = Field(description="verified_fact only when the source report states it as data it retrieved; otherwise hypothesis.")
    source: EvidenceSource = Field(description="Which report or debate the statement comes from.")
    as_of: str | None = Field(default=None, description="Date (YYYY-MM-DD) the underlying data refers to, if the source states one.")


class DecisionProposal(BaseModel):
    """What the formalizer LLM must return. Nothing here is trusted until validated."""

    ticker: str = Field(description="The analysed instrument's ticker, exactly as given.")
    action: Action = Field(description="buy, sell or hold — must agree with the Portfolio Manager's rating.")
    target_weight: float = Field(ge=0.0, le=1.0, description="Proposed position size after the trade, as a fraction of total portfolio equity (0.05 = 5%).")
    thesis: str = Field(min_length=10, description="The investment thesis in two to four sentences.")
    evidence: list[Evidence] = Field(min_length=1, description="Three to eight supporting items, each labelled fact or hypothesis.")
    confidence: float = Field(ge=0.0, le=1.0, description="Probability-style confidence that the call is right over the stated horizon.")
    uncertainty: str = Field(min_length=3, description="What is unknown or could invalidate the thesis.")
    risks: list[str] = Field(min_length=1, description="Key risk considerations.")
    time_horizon: str | None = Field(default=None, description="Intended holding period, e.g. '1-3 months'.")


class DecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class TradeDecision(BaseModel):
    """A validated (or explicitly rejected) research decision. The only input execution reads."""

    model_config = ConfigDict(use_enum_values=False)

    decision_id: str
    run_id: str
    account_id: str
    ticker: str
    rating: Rating
    action: Action
    proposed_target_weight: float | None
    target_weight: float | None
    current_weight: float
    thesis: str
    evidence: list[Evidence]
    confidence: float | None
    uncertainty: str | None
    risks: list[str]
    time_horizon: str | None = None
    decision_timestamp: datetime
    data_timestamps: dict[str, str]
    model_info: dict[str, str | None]
    status: DecisionStatus
    rejection_reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    source: str = "tradingagents"


class Quote(BaseModel):
    ticker: str
    price: float = Field(gt=0)
    timestamp: datetime
    source: str

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=UTC)

    def age_seconds(self, now: datetime) -> float:
        return (now - self.timestamp).total_seconds()


class InstrumentInfo(BaseModel):
    ticker: str
    quote_type: str | None = None     # EQUITY, ETF, ...
    exchange: str | None = None
    name: str | None = None
    currency: str | None = None


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "pending_submit"   # intent persisted, not yet acknowledged by the venue
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"               # blocked by validation or risk before the venue
    FAILED = "failed"                   # venue refused or errored
    CANCELLED = "cancelled"


class RiskViolation(BaseModel):
    code: str
    message: str


class RiskResult(BaseModel):
    approved: bool
    violations: list[RiskViolation] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class OrderIntent(BaseModel):
    """A proposed order before risk; quantities are already the venue quantities."""

    ticker: str
    side: OrderSide
    quantity: float = Field(gt=0)
    requested_quantity: float = Field(gt=0)
    reference_quote: Quote
    estimated_fill_price: float
    estimated_notional: float
    estimated_costs: float
    decision_id: str | None = None
    reason: str = ""


class VenueOrderResult(BaseModel):
    venue: str
    venue_order_id: str | None
    status: str                       # filled / rejected / submitted / error
    reference_price: float | None     # the venue's own fill reference (QuantDinger: last 1m close)
    filled_quantity: float = 0.0
    timestamp: datetime
    replayed: bool = False
    note: str = ""
    raw: dict = Field(default_factory=dict)


class Fill(BaseModel):
    order_id: str
    ticker: str
    side: OrderSide
    quantity: float
    reference_price: float
    fill_price: float
    slippage_bps: float
    commission: float
    fees: float
    notional: float
    timestamp: datetime
    venue: str
    venue_order_id: str | None


class Position(BaseModel):
    ticker: str
    quantity: float
    average_cost: float
    realized_pnl: float = 0.0


class AccountSnapshot(BaseModel):
    account_id: str
    cash: float
    positions: dict[str, Position]
    prices: dict[str, Quote]
    timestamp: datetime

    @property
    def market_value(self) -> float:
        return sum(p.quantity * self.prices[t].price for t, p in self.positions.items() if p.quantity)

    @property
    def equity(self) -> float:
        return self.cash + self.market_value

    def weight(self, ticker: str) -> float:
        pos = self.positions.get(ticker)
        if not pos or not pos.quantity or self.equity <= 0:
            return 0.0
        return pos.quantity * self.prices[ticker].price / self.equity


class SessionInfo(BaseModel):
    session_date: date
    is_open: bool
    opens_at: datetime | None
    closes_at: datetime | None
