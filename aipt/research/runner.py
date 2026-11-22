"""Runs TradingAgents unchanged: analysts → bull/bear debate → research manager → trader →
risk debate → portfolio manager, with this account's holdings as its PortfolioContext."""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from aipt.config import Settings
from aipt.models import Position, utcnow

logger = logging.getLogger(__name__)


def tradingagents_config(settings: Settings) -> dict[str, Any]:
    from tradingagents.default_config import DEFAULT_CONFIG

    base = Path(settings.paths.data_dir) / "tradingagents"
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(
        llm_provider=settings.llm.provider,
        quick_think_llm=settings.llm.quick_model,
        deep_think_llm=settings.llm.deep_model,
        quick_think_provider=settings.llm.quick_provider,
        deep_think_provider=settings.llm.deep_provider,
        max_debate_rounds=settings.research.max_debate_rounds,
        max_risk_discuss_rounds=settings.research.max_risk_discuss_rounds,
        checkpoint_enabled=settings.research.checkpoint_enabled,
        output_language=settings.research.output_language,
        temperature=settings.llm.temperature,
        llm_max_retries=settings.llm.max_retries,
        benchmark_ticker=settings.benchmark.ticker,
        results_dir=str(base / "results"),
        data_cache_dir=str(base / "cache"),
        memory_log_path=str(base / "memory" / "trading_memory.md"),
    )
    return cfg


def portfolio_context(cash: float, positions: dict[str, Position], currency: str = "USD"):
    from tradingagents.portfolio import PortfolioContext
    from tradingagents.portfolio import Position as TAPosition

    return PortfolioContext(
        cash=round(cash, 2), currency=currency,
        positions=[TAPosition(ticker=p.ticker, quantity=p.quantity, average_price=round(p.average_cost, 4))
                   for p in positions.values() if p.quantity])


class TradingAgentsResearcher:
    def __init__(self, settings: Settings, callbacks: list | None = None, graph_factory=None):
        self.settings = settings
        self.callbacks = callbacks or []
        self._graph_factory = graph_factory
        self._graph = None

    @property
    def graph(self):
        if self._graph is None:
            if self._graph_factory is not None:
                self._graph = self._graph_factory(self.settings, self.callbacks)
            else:
                from tradingagents.graph.trading_graph import TradingAgentsGraph

                self._graph = TradingAgentsGraph(selected_analysts=tuple(self.settings.research.analysts),
                                                 config=tradingagents_config(self.settings), callbacks=self.callbacks)
        return self._graph

    def analyze(self, ticker: str, trade_date: str, cash: float, positions: dict[str, Position]) -> dict[str, Any]:
        started: datetime = utcnow()
        state, signal = self.graph.propagate(ticker, trade_date, asset_type="stock",
                                             portfolio=portfolio_context(cash, positions))
        return {"final_state": state, "signal": signal, "started_at": started.isoformat(),
                "finished_at": utcnow().isoformat(), "run_settings": self.graph.run_settings()}
