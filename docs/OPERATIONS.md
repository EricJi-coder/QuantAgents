# Operations

All commands run from the project root with the venv: `source .venv/bin/activate`.
Everything is paper only. No command can submit a real order.

## First-time setup

```bash
git submodule update --init                         # vendor/TradingAgents, vendor/QuantDinger (pinned)
python3.13 -m venv .venv && .venv/bin/pip install -e "vendor/TradingAgents" -e ".[dev]"
# QuantDinger backend (own venv) + PostgreSQL/Redis via Homebrew
brew install postgresql@17 redis
python3.13 -m venv vendor/QuantDinger/backend_api_python/.venv-qd
vendor/QuantDinger/backend_api_python/.venv-qd/bin/pip install -r vendor/QuantDinger/backend_api_python/requirements.txt

scripts/quantdinger_local.sh start                  # Postgres :55432, Redis :56379, API 127.0.0.1:5055
cp .env.example .env && chmod 600 .env
TOKEN=$(scripts/quantdinger_local.sh issue-token)   # paper_only, scopes R,T, market USStock
# put it in .env as QUANTDINGER_AGENT_TOKEN=..., and add ANTHROPIC_API_KEY=... for AI research
aipt qd-check                                        # must show "paper_only": true
aipt init                                            # creates the $100,000 account (idempotent)
```

## Daily operation

| Task | Command |
|---|---|
| Run the scheduler (decision cycle 10:30 ET, close mark 16:10 ET, NYSE days only) | `aipt scheduler` |
| Manual research + trade cycle | `aipt run-cycle [--tickers AAPL,MSFT] [--no-execute]` |
| Resume an interrupted cycle | rerun with the same `--slot` (or just let the scheduler retry) |
| Status (cash, positions, controls, runs, LLM usage) | `aipt status` |
| Reconcile ledger vs QuantDinger | `aipt reconcile` (exit code 1 on issues) |
| Dashboard | `aipt dashboard --serve` → http://127.0.0.1:8765/dashboard.html |
| SavvyTrader queue | `aipt savvy queue`, `aipt savvy confirm <id> --price P --entered-by NAME` |

To keep the scheduler running across reboots on macOS, use a LaunchAgent that runs
`/path/to/.venv/bin/aipt scheduler` with `WorkingDirectory` set to the project. Or use
`docker compose up -d` (needs Docker; see below).

## Safety controls

- **Kill switch:** `aipt kill --reason "..."` blocks all new orders (checked before research,
  before each order, and mid-batch). Add `--venue-stop` to also call QuantDinger's kill
  switch, which revokes trade-scoped tokens. Clear it with
  `aipt resume --reviewer NAME --note "..."`.
- **Drawdown halt:** engages automatically when equity falls 10 % from its running peak.
  Buys are blocked; sells are still allowed. After review, run
  `aipt halt-clear --reviewer NAME --note "..."`; the peak restarts from that point.
- **Budget:** research stops for the day once priced LLM spend reaches
  `research.daily_llm_budget_usd`. Fill in `llm.prices` so spend can be priced.

## Restart and recovery

- Killing the process at any point is safe. Order intents are persisted before the venue
  call, fills are booked atomically, and reruns use the same idempotency keys.
- On start, every cycle runs `recover()`: pending orders from this session are resubmitted
  with their original key, and QuantDinger returns the original fill if it already executed
  one. Pending orders from an earlier session are cancelled, never filled late.
- If `aipt reconcile` reports issues, don't clear the kill switch until they're understood.
  The audit log (`audit_log` table) and `data/logs/aipt.jsonl` record every transition.

## Files

| Path | Contents |
|---|---|
| `data/ledger.db` | authoritative paper ledger (back this up) |
| `data/logs/aipt.jsonl` | structured logs |
| `data/tradingagents/` | TradingAgents checkpoints, memory log, per-run state logs |
| `reports/runs/*.json` | one report per cycle: decisions, orders, rejections, reconciliation |
| `reports/dashboard.html` | dashboard |
| `reports/savvytrader/` | publication report, record and trade-entry queue |
| `data/backtests/<name>/`, `data/replays/<date>/` | separate HISTORICAL_BACKTEST / REPLAY_DEMO ledgers |
| `var/quantdinger/` | local QuantDinger data, logs and generated credentials (mode 600) |

## Tests

```bash
pytest                    # offline suite: no network, no QuantDinger, no LLM
pytest -m quantdinger     # read-only checks against the running local QuantDinger
```

## Docker

`docker compose up -d --build` runs the scheduler and a dashboard server. QuantDinger runs
separately, either natively via `scripts/quantdinger_local.sh` (reached at
`host.docker.internal:5055`) or with QuantDinger's own compose with
`LIVE_TRADING_ENABLED=false` and `AGENT_LIVE_TRADING_ENABLED=false`. With the native setup,
QuantDinger binds to 127.0.0.1, so set `PYTHON_API_HOST=0.0.0.0` in
`var/quantdinger/qd.env` only if containers need to reach it, and keep the host firewall on.
