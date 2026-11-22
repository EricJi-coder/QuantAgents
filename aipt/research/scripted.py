"""Offline research mode: the real TradingAgents graph driven by a deterministic rule.

This exists so the full pipeline (graph → formalizer → validation → risk → execution) can be
exercised without LLM credentials. It is NOT AI research: every agent's "answer" is a fixed
template filled from a price-momentum rule computed on point-in-time daily bars, and every
decision it produces is labelled ``SCRIPTED (not AI research)``.

The graph is built with ``trading_graph.create_tier_client`` patched for the duration of the
constructor only; TradingAgents' source is not modified.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from unittest import mock

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from aipt.config import Settings
from aipt.models import (
    Action,
    DecisionProposal,
    Evidence,
    EvidenceKind,
    EvidenceSource,
    RATING_DIRECTION,
    Rating,
)

LABEL = "SCRIPTED (not AI research)"
_TRANSACTION = {Action.BUY: "BUY", Action.HOLD: "HOLD", Action.SELL: "SELL"}


@dataclass
class RuleView:
    ticker: str
    as_of: str
    close: float
    sma50: float
    ret20: float
    rating: Rating

    def facts(self) -> str:
        return (f"{self.ticker} closed at {self.close:.2f} on {self.as_of}; 50-day average {self.sma50:.2f}; "
                f"20-session return {self.ret20:+.2%}.")


def momentum_view(market_data, ticker: str, trade_date: date, held: bool) -> RuleView:
    bars = market_data.daily_bars(ticker, trade_date - timedelta(days=120), trade_date)
    closes = bars["close"]
    if len(closes) < 50:
        raise ValueError(f"not enough history for {ticker} ({len(closes)} sessions)")
    close, sma50 = float(closes.iloc[-1]), float(closes.tail(50).mean())
    ret20 = float(closes.iloc[-1] / closes.iloc[-21] - 1)
    if close > sma50 and ret20 > 0.02:
        rating = Rating.OVERWEIGHT
    elif close < sma50 and ret20 < -0.03:
        rating = Rating.SELL if held and ret20 < -0.08 else Rating.UNDERWEIGHT
    else:
        rating = Rating.HOLD
    return RuleView(ticker, bars.index[-1].date().isoformat(), close, sma50, ret20, rating)


class ScriptedChatModel(BaseChatModel):
    """Answers every agent with the current rule view. No tool calls, no structured output."""

    script: dict = Field(default_factory=dict)  # shared across bound copies; set per ticker

    @property
    def _llm_type(self) -> str:
        return "scripted-rule"

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        raise NotImplementedError("scripted model answers in free text")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        view: RuleView = self.script[threading.get_ident()] if threading.get_ident() in self.script \
            else self.script["current"]
        direction = RATING_DIRECTION[view.rating]
        text = (f"[{LABEL}] {view.facts()} Rule: above the 50-day average with a 20-session gain over 2% is "
                f"Overweight; below it with a loss over 3% is Underweight (Sell if held and the loss exceeds 8%); "
                f"otherwise Hold.\n\n**Rating**: {view.rating.value}\n\n"
                f"FINAL TRANSACTION PROPOSAL: **{_TRANSACTION[direction]}**")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


class _Client:
    def __init__(self, model):
        self.model = model

    def get_llm(self):
        return self.model


class ScriptedResearcher:
    """Drop-in for TradingAgentsResearcher that runs the real graph with ScriptedChatModel."""

    def __init__(self, settings: Settings, market_data, callbacks: list | None = None):
        self.settings = settings
        self.md = market_data
        self.callbacks = callbacks or []
        self.model = ScriptedChatModel()
        self._graph = None
        self.last_view: RuleView | None = None

    @property
    def graph(self):
        if self._graph is None:
            from tradingagents.graph import trading_graph

            from aipt.research.runner import tradingagents_config

            cfg = tradingagents_config(self.settings)
            cfg["checkpoint_enabled"] = False
            # The social analyst pre-fetches Reddit/StockTwits itself (rate-limited, slow) and the
            # scripted model would not read it; the other analysts, debates and managers all run.
            analysts = tuple(a for a in self.settings.research.analysts if a != "social") or ("market",)
            with mock.patch.object(trading_graph, "create_tier_client",
                                   lambda config, tier, **k: _Client(self.model)):
                self._graph = trading_graph.TradingAgentsGraph(
                    selected_analysts=analysts, config=cfg, callbacks=self.callbacks)
        return self._graph

    def analyze(self, ticker: str, trade_date: str, cash: float, positions) -> dict[str, Any]:
        from aipt.models import utcnow
        from aipt.research.runner import portfolio_context

        view = momentum_view(self.md, ticker, date.fromisoformat(trade_date), held=ticker in positions)
        self.last_view = view
        self.model.script["current"] = view
        started = utcnow()
        state, signal = self.graph.propagate(ticker, trade_date, asset_type="stock",
                                             portfolio=portfolio_context(cash, positions))
        return {"final_state": state, "signal": signal, "started_at": started.isoformat(),
                "finished_at": utcnow().isoformat(), "run_settings": {"mode": LABEL}, "rule_view": view}


class ScriptedFormalizer:
    """Deterministic formalizer for scripted mode: builds the proposal from the rule view."""

    def __init__(self, settings: Settings, researcher: ScriptedResearcher):
        self.settings = settings
        self.researcher = researcher

    def model_name(self) -> str:
        return LABEL

    def formalize(self, ticker: str, trade_date: str, final_state: dict[str, Any],
                  current_weight: float) -> tuple[DecisionProposal | None, str | None]:
        view = self.researcher.last_view
        if view is None or view.ticker != ticker:
            return None, "no rule view for this ticker"
        rating = Rating(final_state.get("final_rating") or view.rating.value)
        action = RATING_DIRECTION.get(rating, Action.HOLD)
        target = {Rating.BUY: self.settings.allocation.default_buy_weight,
                  Rating.OVERWEIGHT: self.settings.allocation.default_buy_weight,
                  Rating.HOLD: current_weight,
                  Rating.UNDERWEIGHT: current_weight * self.settings.allocation.underweight_fraction,
                  Rating.SELL: 0.0}.get(rating, current_weight)
        strength = min(abs(view.ret20) / 0.10, 1.0)
        return DecisionProposal(
            ticker=ticker, action=action, target_weight=round(target, 4),
            thesis=f"[{LABEL}] {rating.value}: {view.facts()}",
            evidence=[
                Evidence(statement=view.facts(), kind=EvidenceKind.VERIFIED_FACT,
                         source=EvidenceSource.MARKET_REPORT, as_of=view.as_of),
                Evidence(statement="Recent momentum persists over the next weeks.", kind=EvidenceKind.HYPOTHESIS,
                         source=EvidenceSource.PORTFOLIO_MANAGER),
            ],
            confidence=round(0.5 + 0.2 * strength, 3),
            uncertainty="A price rule ignores fundamentals, news and sentiment; momentum can reverse abruptly.",
            risks=["momentum reversal", "market-wide drawdown"], time_horizon="2-6 weeks",
        ), None
