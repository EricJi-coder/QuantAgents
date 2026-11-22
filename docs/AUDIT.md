# Phase 1 — Repository audit (2026-11-22)

| | TradingAgents | QuantDinger |
|---|---|---|
| Source | github.com/TauricResearch/TradingAgents | github.com/OpenByteInc/QuantDinger |
| Vendored at | `vendor/TradingAgents` @ `9fa1196` (2026-11-04), v0.7.1 | `vendor/QuantDinger` @ `8b5b871` (2026-11-22) |
| Language / runtime | Python ≥ 3.11, LangGraph + LangChain | Python 3.12 Flask API + Vue UI, PostgreSQL, Redis, optional Kafka/Celery |
| Own tests | Suite passes in this environment (Python 3.13) | Not run (needs its full service stack) |

## TradingAgents — what is reused

- **The whole research graph, unmodified**: market / social (sentiment) / news / fundamentals
  analysts → bull and bear researchers debate → Research Manager → Trader → aggressive /
  conservative / neutral risk debate → Portfolio Manager. `TradingAgentsGraph.propagate()`
  returns the full final state plus the Portfolio Manager's 5-tier rating
  (Buy / Overweight / Hold / Underweight / Sell).
- **`PortfolioContext`**: the account's real cash and holdings go into every run.
- **LLM clients**: `tradingagents/llm_clients` supports `anthropic`, `openai`, `google`,
  `azure`, `bedrock` and OpenAI-compatible providers (xAI, DeepSeek, OpenRouter, Ollama …).
  It has two model **tiers**: `quick_think_llm` (analysts, researchers, debaters, trader)
  and `deep_think_llm` (Research Manager, Portfolio Manager), each with its own provider.
- **Checkpointing, memory log, results logs**: enabled and pointed into `data/`.

Gaps found:
- The rating is typed, but sizing, evidence and confidence come back as free text. aipt adds
  a **formalizer** (schema-bound structured output) and a deterministic validator.
- Model choice is per tier, not per agent. Routing per agent would mean forking
  `GraphSetup`, so aipt exposes the two tiers plus a separate formalizer model.
- Data vendor defaults to yfinance; point-in-time behaviour for historical dates depends on
  each tool's date filtering. aipt's own validator rejects evidence dated after the analysis
  date, and backtests flag LLM-training contamination (see ARCHITECTURE.md).

## QuantDinger — what is reused

- **Agent Gateway** (`/api/agent/v1`, documented in `docs/agent/agent-openapi.json`): scoped
  agent tokens (`R`, `T` scopes, market allow-list, `paper_only`, per-order and per-day
  notional caps, rate limit), `whoami`, `klines`, `price`,
  `POST /quick-trade/orders` with `Idempotency-Key` (replays return the original result),
  `GET /portfolio/paper-orders`, `POST /quick-trade/kill-switch`.
  This is the integration surface. QuantDinger's core engine is not modified.
- **`app/services/market_schedule.py`** (exchange-calendar sessions) and
  **`app/services/backtest/metrics.py`** (benchmark-relative metrics: beta, alpha,
  information ratio, capture, Sortino). They are imported directly without building the
  Flask app (`aipt/vendor.py`).

Findings that shaped the adapter:
- **Paper fills** are recorded in `qd_agent_paper_orders` at the latest 1-minute close with
  no staleness, slippage or fee model. aipt adds quote-age and session checks before
  submitting, and applies its own slippage/fee model to QuantDinger's reference price.
- **`LIVE_TRADING_ENABLED` defaults to true** in QuantDinger's settings. The local bootstrap
  sets `LIVE_TRADING_ENABLED=false` and `AGENT_LIVE_TRADING_ENABLED=false`, and the token is
  issued `paper_only=true`.
- **Token defaults** are $1,000 per order / $5,000 per day, which blocks normal positions in a
  $100k account. `issue-token` sets $15,000 / $150,000 (env-overridable). aipt's risk engine
  remains the primary limit.
- `GET /portfolio/paper-orders` returns only the newest 200 records per user. Reconciliation
  accounts for that truncation.
- Port 5000 is taken by macOS AirPlay; the local API runs on 127.0.0.1:5055.

## Dependencies and compatibility

- One Python 3.13 venv (`.venv`) runs TradingAgents 0.7.1 + aipt; QuantDinger's backend has
  its own venv (`vendor/QuantDinger/backend_api_python/.venv-qd`) and runs as a separate
  process. They communicate only over HTTP.
- Native services via Homebrew: PostgreSQL 17, Redis (or QuantDinger's own Docker compose).

## Security notes

- Secrets only from the environment / `.env` (mode 600, git-ignored). The agent token is never
  logged (`repr` hides it); QuantDinger admin credentials are generated randomly into
  `var/quantdinger/qd.env` (mode 600).
- QuantDinger listens on 127.0.0.1 only. Postgres listens on TCP 127.0.0.1 with Unix sockets
  disabled (the project path contains a space).
- SavvyTrader: no API; its Terms forbid automated access, and robots.txt disallows
  `api.savvytrader.com`. Nothing in aipt contacts SavvyTrader (see SAVVYTRADER.md).
