"""Assembles a TradingSystem from settings for live-forward paper use or a replay."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from aipt.clock import Clock, SystemClock
from aipt.config import Secrets, Settings
from aipt.costs import UsageTracker
from aipt.execution.adapter import fill_reference_for_replay, fill_reference_live
from aipt.execution.venues import LocalPaperVenue, QuantDingerClient, QuantDingerPaperVenue
from aipt.ledger import Ledger
from aipt.market.calendar import USEquityCalendar
from aipt.market.data import MarketData, QuantDingerMarketData, YahooMarketData
from aipt.workflow.cycle import TradingSystem

ResearchMode = Literal["tradingagents", "scripted"]

_PROVIDER_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY",
                  "xai": "XAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


class MissingCredentials(RuntimeError):
    pass


def required_llm_keys(settings: Settings) -> dict[str, str]:
    cfg = settings.llm
    providers = {cfg.quick_provider or cfg.provider, cfg.deep_provider or cfg.provider,
                 cfg.formalizer_provider or cfg.deep_provider or cfg.provider}
    return {p: _PROVIDER_KEYS[p] for p in providers if p in _PROVIDER_KEYS}


def check_llm_credentials(settings: Settings) -> None:
    missing = [env for env in required_llm_keys(settings).values() if not os.getenv(env)]
    if missing:
        raise MissingCredentials(
            f"TradingAgents research needs {', '.join(missing)} in the environment (or .env). Create a key in the "
            f"provider's console (Anthropic: https://console.anthropic.com/settings/keys) and add "
            f"`{missing[0]}=...` to .env. "
            f"Without a key, use `--research scripted` for an offline, rule-driven (non-AI) run.")


def build_researchers(settings: Settings, mode: ResearchMode, market_data: MarketData, tracker: UsageTracker):
    if mode == "scripted":
        from aipt.research.scripted import ScriptedFormalizer, ScriptedResearcher

        researcher = ScriptedResearcher(settings, market_data, callbacks=[tracker])
        return researcher, ScriptedFormalizer(settings, researcher)
    check_llm_credentials(settings)
    from aipt.research.formalizer import DecisionFormalizer
    from aipt.research.runner import TradingAgentsResearcher

    return (TradingAgentsResearcher(settings, callbacks=[tracker]),
            DecisionFormalizer(settings, callbacks=[tracker]))


def build_live(settings: Settings, *, research: ResearchMode = "tradingagents", clock: Clock | None = None,
               venue_kind: str | None = None, with_research: bool = True) -> TradingSystem:
    """Live-forward paper trading: wall clock, live quotes, QuantDinger (or local) paper venue."""
    clock = clock or SystemClock()
    venue_kind = venue_kind or settings.execution.venue
    yahoo = YahooMarketData(clock)
    if venue_kind == "quantdinger":
        secrets = Secrets.from_env()
        if not (secrets.quantdinger_base_url and secrets.quantdinger_agent_token):
            raise MissingCredentials("QUANTDINGER_BASE_URL and QUANTDINGER_AGENT_TOKEN must be set "
                                     "(run scripts/quantdinger_local.sh start && issue-token)")
        client = QuantDingerClient(secrets.quantdinger_base_url, secrets.quantdinger_agent_token)
        venue = QuantDingerPaperVenue(client, settings.execution.quantdinger_market)
        venue.verify_paper_only()
        market_data: MarketData = QuantDingerMarketData(client, clock, fallback=yahoo,
                                                        market=settings.execution.quantdinger_market)
    else:
        market_data = yahoo
        venue = LocalPaperVenue(Path(settings.paths.data_dir) / "local_venue.db", fill_reference_live(yahoo))
    tracker = UsageTracker(settings.llm.prices)
    researcher, formalizer = build_researchers(settings, research, market_data, tracker) if with_research \
        else (None, None)
    ledger = Ledger(settings.ledger_path, settings.account.id)
    return TradingSystem(settings, clock=clock, ledger=ledger, market_data=market_data, venue=venue,
                         researcher=researcher, formalizer=formalizer, tracker=tracker)


def build_replay(settings: Settings, *, store, clock, research: ResearchMode = "scripted",
                 label_dir: str = "replay", market_on_open: bool = False) -> TradingSystem:
    """Simulated clock over recorded bars. Fills at the next traded price after the order, or,
    with ``market_on_open`` (daily-bar backtests), at the session's official open."""
    from aipt.market.data import PointInTimeMarketData

    calendar = USEquityCalendar()
    md = PointInTimeMarketData(store, clock, calendar)
    reference = fill_reference_live(md) if market_on_open else \
        fill_reference_for_replay(md, clock, settings.execution.execution_latency_seconds)
    venue = LocalPaperVenue(Path(settings.paths.data_dir) / f"{label_dir}_venue.db", reference)
    tracker = UsageTracker(settings.llm.prices)
    researcher, formalizer = build_researchers(settings, research, md, tracker)
    ledger = Ledger(settings.ledger_path, settings.account.id)
    return TradingSystem(settings, clock=clock, ledger=ledger, market_data=md, venue=venue,
                         researcher=researcher, formalizer=formalizer, tracker=tracker, calendar=calendar)
