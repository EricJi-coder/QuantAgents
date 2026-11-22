"""Decision formalizer: turns a finished TradingAgents run into a DecisionProposal.

This is an extra structured-output call after the graph, not a change to it. It reads the
Portfolio Manager's decision and the reports it rests on and fills a strict schema (target
weight, confidence, evidence labelled fact vs hypothesis, risks). Its output is a proposal:
``aipt.research.decision.validate`` decides whether it becomes a TradeDecision.
"""

from __future__ import annotations

import logging
from typing import Any

from aipt.config import Settings
from aipt.models import DecisionProposal

logger = logging.getLogger(__name__)

_REPORT_LIMIT = 6000


def _clip(text: str | None, limit: int = _REPORT_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n[... truncated ...]"


def build_prompt(ticker: str, trade_date: str, final_state: dict[str, Any], current_weight: float,
                 max_position_weight: float) -> str:
    debate = final_state.get("investment_debate_state") or {}
    risk = final_state.get("risk_debate_state") or {}
    return f"""You convert a completed multi-agent equity research run into a machine-readable trading decision.

Instrument: {ticker} (US-listed). Analysis date: {trade_date}. All data in the reports is as of that date.
Current position: {current_weight:.2%} of portfolio equity. Portfolio policy cap per security: {max_position_weight:.0%}.

The Portfolio Manager's final rating is binding for direction:
- Buy or Overweight -> action "buy" (target_weight above the current weight)
- Hold -> action "hold" (target_weight equal to the current weight)
- Underweight -> action "sell" (target_weight below the current weight, above zero)
- Sell -> action "sell" (target_weight 0)

Rules:
1. Do not invent facts. Every evidence item must come from the material below; name its source.
2. Label an item "verified_fact" only if the source report states it as retrieved data (a price, a filed
   figure, a dated headline). Forecasts, interpretations, debate arguments and opinions are "hypothesis".
3. Give as_of only when the source states the date the data refers to; never a date after {trade_date}.
4. confidence is your probability that the call is right over the stated horizon, reflecting the strength of
   the evidence and the disagreement in the debates. Do not overstate it.
5. target_weight is a fraction of total equity (0.05 = 5%). Respect the direction rule above.
Use only the evidence provided in this prompt. Do not call external tools or search the web.

=== PORTFOLIO MANAGER FINAL DECISION ===
{_clip(final_state.get("final_trade_decision"))}

=== TRADER PROPOSAL ===
{_clip(final_state.get("trader_investment_plan"), 3000)}

=== RESEARCH MANAGER PLAN ===
{_clip(final_state.get("investment_plan"), 3000)}

=== MARKET (TECHNICAL) REPORT ===
{_clip(final_state.get("market_report"))}

=== FUNDAMENTALS REPORT ===
{_clip(final_state.get("fundamentals_report"))}

=== NEWS REPORT ===
{_clip(final_state.get("news_report"))}

=== SENTIMENT REPORT ===
{_clip(final_state.get("sentiment_report"))}

=== BULL/BEAR DEBATE (latest) ===
{_clip(debate.get("history"), 4000)}

=== RISK DEBATE (latest) ===
{_clip(risk.get("history"), 4000)}
"""


class DecisionFormalizer:
    def __init__(self, settings: Settings, llm=None, callbacks: list | None = None):
        self.settings = settings
        self._llm = llm
        self.callbacks = callbacks or []

    @property
    def llm(self):
        if self._llm is None:
            from tradingagents.llm_clients.factory import build_llm_kwargs, create_llm_client

            cfg = self.settings.llm
            provider = cfg.formalizer_provider or cfg.deep_provider or cfg.provider
            model = cfg.formalizer_model or cfg.deep_model
            kwargs = build_llm_kwargs({"llm_provider": provider, "temperature": cfg.temperature,
                                       "llm_max_retries": cfg.max_retries})
            if self.callbacks:
                kwargs["callbacks"] = self.callbacks
            self._llm = create_llm_client(provider=provider, model=model, **kwargs).get_llm()
        return self._llm

    def model_name(self) -> str:
        cfg = self.settings.llm
        return cfg.formalizer_model or cfg.deep_model

    def formalize(self, ticker: str, trade_date: str, final_state: dict[str, Any],
                  current_weight: float) -> tuple[DecisionProposal | None, str | None]:
        from tradingagents.agents.structured import bind_structured

        prompt = build_prompt(ticker, trade_date, final_state, current_weight,
                              self.settings.risk.max_position_weight)
        structured = bind_structured(self.llm, DecisionProposal, "Decision Formalizer")
        if structured is None:
            return None, "the configured model does not support structured output"
        try:
            result = structured.invoke(prompt)
        except Exception as exc:  # malformed output or provider error: no decision, never a guess
            logger.warning("formalizer failed for %s: %s", ticker, exc)
            return None, f"structured output failed: {type(exc).__name__}: {exc}"
        if result is None:
            return None, "structured output returned nothing"
        if isinstance(result, dict):
            result = DecisionProposal.model_validate(result)
        return result, None
