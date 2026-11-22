"""Typed configuration: config/default.yaml, an optional override file, then secrets from env.

``mode`` is a Literal["paper"]: there is no configuration value that enables real-money
trading, so a typo or a malicious override fails validation instead of going live.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "default.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountConfig(_Strict):
    id: str
    name: str
    initial_capital: float = Field(gt=0)
    base_currency: Literal["USD"] = "USD"
    universe: Literal["us_equities_and_etfs"] = "us_equities_and_etfs"


class PathsConfig(_Strict):
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")


class RiskConfig(_Strict):
    max_position_weight: float = Field(0.10, gt=0, le=1)
    max_gross_exposure: float = Field(1.0, gt=0, le=1.0)   # le=1.0: no leverage, ever
    min_cash_reserve: float = Field(0.05, ge=0, lt=1)
    max_daily_buy_turnover: float = Field(0.20, gt=0, le=1)
    drawdown_halt: float = Field(0.10, gt=0, lt=1)
    min_order_notional: float = Field(100.0, ge=0)
    max_orders_per_ticker_per_session: int = Field(1, ge=1)
    max_quote_age_seconds: int = Field(1200, gt=0)
    session_open_buffer_minutes: int = Field(5, ge=0)
    session_close_buffer_minutes: int = Field(5, ge=0)
    allow_fractional_shares: bool = False
    denied_tickers: list[str] = Field(default_factory=list)
    denied_name_patterns: list[str] = Field(default_factory=list)

    @field_validator("denied_tickers")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [t.strip().upper() for t in v]


class AllocationConfig(_Strict):
    min_confidence_for_buy: float = Field(0.55, ge=0, le=1)
    default_buy_weight: float = Field(0.05, gt=0, le=1)
    underweight_fraction: float = Field(0.5, gt=0, lt=1)


class ExecutionConfig(_Strict):
    venue: Literal["quantdinger", "local"] = "quantdinger"
    quantdinger_market: str = "USStock"
    order_type: Literal["market"] = "market"
    half_spread_bps: float = Field(3.0, ge=0)
    impact_bps_per_pct_adv: float = Field(5.0, ge=0)
    max_slippage_bps: float = Field(50.0, ge=0)
    max_participation_of_adv: float = Field(0.01, gt=0, le=1)
    commission_per_share: float = Field(0.0, ge=0)
    commission_min: float = Field(0.0, ge=0)
    sec_fee_per_million: float = Field(27.80, ge=0)
    finra_taf_per_share: float = Field(0.000166, ge=0)
    finra_taf_max: float = Field(8.30, ge=0)
    execution_latency_seconds: int = Field(2, ge=0)
    max_reference_price_deviation: float = Field(0.02, gt=0)


class ResearchConfig(_Strict):
    watchlist: list[str] = Field(default_factory=list)
    max_tickers_per_cycle: int = Field(4, ge=1)
    analysts: list[Literal["market", "social", "news", "fundamentals"]] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"])
    max_debate_rounds: int = Field(1, ge=1)
    max_risk_discuss_rounds: int = Field(1, ge=1)
    checkpoint_enabled: bool = True
    daily_llm_budget_usd: float = Field(25.0, ge=0)
    output_language: str = "English"

    @field_validator("watchlist")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [t.strip().upper() for t in v]


class PriceEntry(_Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)


class LLMConfig(_Strict):
    provider: str = "anthropic"
    quick_model: str = "claude-sonnet-5"
    deep_model: str = "claude-opus-5-5"
    quick_provider: str | None = None
    deep_provider: str | None = None
    formalizer_provider: str | None = None
    formalizer_model: str | None = None
    temperature: float | None = None
    max_retries: int | None = 3
    knowledge_cutoff: str | None = None
    prices: dict[str, PriceEntry] = Field(default_factory=dict)


class ScheduleConfig(_Strict):
    timezone: str = "America/New_York"
    decision_times: list[str] = Field(default_factory=lambda: ["10:30"])
    mark_to_market_time: str = "16:10"
    poll_seconds: int = Field(30, ge=1)


class BenchmarkConfig(_Strict):
    ticker: str = "SPY"
    risk_free_rate: float = 0.0


class SavvyTraderConfig(_Strict):
    enabled: bool = True
    portfolio_name: str = "AI Multi-Agent Research Portfolio (SIMULATED)"
    disclaimer: str


class PublishingConfig(_Strict):
    savvytrader: SavvyTraderConfig


class Secrets(BaseModel):
    """Read from the environment only; never written to disk or logs by this package."""

    quantdinger_base_url: str | None = None
    quantdinger_agent_token: str | None = Field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> Secrets:
        return cls(
            quantdinger_base_url=os.getenv("QUANTDINGER_BASE_URL") or None,
            quantdinger_agent_token=os.getenv("QUANTDINGER_AGENT_TOKEN") or None,
        )


class Settings(_Strict):
    mode: Literal["paper"]
    account: AccountConfig
    paths: PathsConfig = PathsConfig()
    risk: RiskConfig = RiskConfig()
    allocation: AllocationConfig = AllocationConfig()
    execution: ExecutionConfig = ExecutionConfig()
    research: ResearchConfig = ResearchConfig()
    llm: LLMConfig = LLMConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    benchmark: BenchmarkConfig = BenchmarkConfig()
    publishing: PublishingConfig

    def resolve(self, base: Path = ROOT) -> Settings:
        """Make relative paths absolute against the project root."""
        paths = PathsConfig(
            data_dir=self.paths.data_dir if self.paths.data_dir.is_absolute() else base / self.paths.data_dir,
            reports_dir=self.paths.reports_dir if self.paths.reports_dir.is_absolute() else base / self.paths.reports_dir,
        )
        return self.model_copy(update={"paths": paths})

    @property
    def ledger_path(self) -> Path:
        return self.paths.data_dir / "ledger.db"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_dotenv_file(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding what is already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings(override_path: str | Path | None = None, overrides: dict[str, Any] | None = None,
                  base: Path = ROOT) -> Settings:
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    if override_path:
        data = _deep_merge(data, yaml.safe_load(Path(override_path).read_text(encoding="utf-8")) or {})
    if overrides:
        data = _deep_merge(data, overrides)
    return Settings.model_validate(data).resolve(base)
