"""Structured validation: the gate between LLM output and anything executable.

A decision is accepted only if every deterministic check passes; otherwise it is stored as
rejected with its reasons and produces no order. Free-form text is never parsed into an
order: the direction comes from the Portfolio Manager's typed rating, the size from the
schema-validated proposal, and the two must agree.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from aipt.config import AllocationConfig
from aipt.models import (
    RATING_DIRECTION,
    Action,
    DecisionProposal,
    DecisionStatus,
    EvidenceKind,
    EvidenceSource,
    Rating,
    TradeDecision,
)

_SOURCE_FIELDS = {
    EvidenceSource.MARKET_REPORT: "market_report",
    EvidenceSource.SENTIMENT_REPORT: "sentiment_report",
    EvidenceSource.NEWS_REPORT: "news_report",
    EvidenceSource.FUNDAMENTALS_REPORT: "fundamentals_report",
    EvidenceSource.PORTFOLIO_MANAGER: "final_trade_decision",
}

_EPS = 1e-6


def _rating(final_state: dict[str, Any], signal: str | None) -> Rating:
    value = final_state.get("final_rating") or signal or "REVIEW"
    try:
        return Rating(value)
    except ValueError:
        return Rating.REVIEW


def validate(*, run_id: str, account_id: str, ticker: str, trade_date: str, final_state: dict[str, Any],
             signal: str | None, proposal: DecisionProposal | None, formalizer_error: str | None,
             current_weight: float, allocation: AllocationConfig, now: datetime,
             data_timestamps: dict[str, str], model_info: dict[str, str | None]) -> TradeDecision:
    reasons: list[str] = []
    notes: list[str] = []
    rating = _rating(final_state, signal)

    if rating is Rating.REVIEW:
        reasons.append("Portfolio Manager produced no parseable rating (REVIEW); not tradeable")
    if proposal is None:
        reasons.append(f"formalizer produced no structured decision: {formalizer_error or 'unknown error'}")

    expected = RATING_DIRECTION.get(rating)
    action = expected or Action.HOLD
    target: float | None = None

    if proposal is not None:
        if proposal.ticker.strip().upper() != ticker.upper():
            reasons.append(f"proposal is for {proposal.ticker!r}, not {ticker}")
        if expected is not None and proposal.action is not expected:
            reasons.append(f"proposal action {proposal.action.value!r} contradicts the Portfolio Manager's "
                           f"{rating.value} rating (expected {expected.value!r})")
        if not any(e.kind is EvidenceKind.VERIFIED_FACT for e in proposal.evidence):
            reasons.append("no evidence item is a verified fact; a decision needs at least one")
        for e in proposal.evidence:
            field = _SOURCE_FIELDS.get(e.source)
            if field and not (final_state.get(field) or "").strip():
                reasons.append(f"evidence cites {e.source.value}, which is empty in this run")
            if e.as_of and e.as_of[:10] > trade_date:
                reasons.append(f"evidence dated {e.as_of} is after the analysis date {trade_date} (look-ahead)")

    if not reasons and proposal is not None and expected is not None:
        proposed = proposal.target_weight
        if rating in (Rating.BUY, Rating.OVERWEIGHT):
            if proposal.confidence < allocation.min_confidence_for_buy:
                action, target = Action.HOLD, current_weight
                notes.append(f"confidence {proposal.confidence:.2f} below buy threshold "
                             f"{allocation.min_confidence_for_buy:.2f}; holding")
            elif proposed > current_weight + _EPS:
                target = proposed
            else:
                action, target = Action.HOLD, current_weight
                notes.append(f"{rating.value} but proposed weight {proposed:.2%} does not exceed current "
                             f"{current_weight:.2%}; holding")
        elif rating is Rating.HOLD:
            target = current_weight
        elif rating is Rating.UNDERWEIGHT:
            if current_weight <= _EPS:
                action, target = Action.HOLD, 0.0
                notes.append("Underweight with no position; nothing to reduce")
            elif _EPS < proposed < current_weight - _EPS:
                target = proposed
            else:
                target = current_weight * allocation.underweight_fraction
                notes.append(f"proposed weight {proposed:.2%} is not a reduction; using "
                             f"{allocation.underweight_fraction:.0%} of the current position")
        elif rating is Rating.SELL:
            if current_weight <= _EPS:
                action, target = Action.HOLD, 0.0
                notes.append("Sell with no position; nothing to sell (no short selling)")
            else:
                target = 0.0

    status = DecisionStatus.REJECTED if reasons else DecisionStatus.ACCEPTED
    return TradeDecision(
        decision_id=f"dec_{uuid.uuid4().hex}",
        run_id=run_id, account_id=account_id, ticker=ticker, rating=rating,
        action=action if status is DecisionStatus.ACCEPTED else Action.HOLD,
        proposed_target_weight=proposal.target_weight if proposal else None,
        target_weight=target if status is DecisionStatus.ACCEPTED else None,
        current_weight=current_weight,
        thesis=proposal.thesis if proposal else (final_state.get("final_trade_decision") or "")[:2000],
        evidence=proposal.evidence if proposal else [],
        confidence=proposal.confidence if proposal else None,
        uncertainty=proposal.uncertainty if proposal else None,
        risks=proposal.risks if proposal else [],
        time_horizon=proposal.time_horizon if proposal else None,
        decision_timestamp=now, data_timestamps=data_timestamps, model_info=model_info,
        status=status, rejection_reasons=reasons, notes=notes,
    )
