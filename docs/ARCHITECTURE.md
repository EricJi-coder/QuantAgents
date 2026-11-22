# Architecture

```
                ┌───────────────────────── aipt (this repo) ─────────────────────────┐
 schedule ─────►│ workflow/cycle.py  TradingSystem.run_cycle(slot)                   │
 (10:30 ET)     │   1 load account (ledger)          8 submit paper orders ─────────┼──► QuantDinger Agent Gateway
 manual run ───►│   2 quotes (QuantDinger / Yahoo)   9 book fills (fill model)      │    POST /quick-trade/orders
                │   3 research ──► TradingAgents graph (unchanged)                  │    (paper_only token,
                │   4 formalize ─► DecisionProposal (structured output)             │     Idempotency-Key)
                │   5 validate ──► TradeDecision accepted / rejected                │
                │   6 construct ─► OrderIntents (sells first, sized at fresh quote) │
                │   7 risk ──────► RiskEngine (blocks; never resizes)               │
                │  10 reconcile   11 performance   12 SavvyTrader queue + report    │
                │  13 run report, logs, audit log                                   │
                └───────────────────────────────┬───────────────────────────────────┘
                                                │
                                  data/ledger.db (SQLite, WAL) — authoritative
```

## Ownership of state

| State | Owner | Notes |
|---|---|---|
| Cash, positions, average cost, realized P&L, dividends | **aipt ledger** (`data/ledger.db`) | Double-checked by reconciliation every cycle |
| Decisions, orders (incl. rejected with reasons), fills, equity snapshots, controls, runs, LLM usage, audit log | aipt ledger | Append-only except order status transitions |
| Paper order records and the venue reference price | QuantDinger (`qd_agent_paper_orders`) | Reconciled against ledger fills |
| Public trade record and SavvyTrader's own execution prices | SavvyTrader (entered manually) | Recorded back into `publication_queue`; never replaces ledger data |

QuantDinger is the **execution venue**. It does not keep a cash or position account for agent
paper orders, so the aipt ledger is the accounting system. Reconciliation verifies:
positions = Σ fills; cash = initial + trade cash flows + dividends; equity = initial +
dividends + realized + unrealized; and every ledger fill matches a QuantDinger paper order
(symbol, side, quantity, reference price). It also flags any QuantDinger fill the ledger
doesn't know about.

## Interfaces

- `MarketData` protocol (`market/data.py`): `quote`, `adv_shares`, `daily_bars`, `dividends`,
  `instrument_info`. Every price carries its timestamp. Implementations:
  `QuantDingerMarketData` (gateway 1-minute klines; Yahoo fallback), `YahooMarketData`, and
  `PointInTimeMarketData` (replays/backtests; never returns a bar that ends after the
  simulated clock).
- `PaperVenue` protocol (`execution/venues.py`): `submit(idempotency_key, ticker, side,
  quantity)`, `list_orders`, `emergency_stop`. Implementations: `QuantDingerPaperVenue`
  (production) and `LocalPaperVenue` (offline emulation of the same contract for tests,
  replays and backtests).
- Researcher: `analyze(ticker, trade_date, cash, positions) → final_state, signal`.
  Formalizer: `formalize(...) → DecisionProposal | error`.

## Decision schema (machine-readable)

`TradeDecision` (`models.py`), stored as JSON in `decisions.payload`:
`decision_id`, `run_id`, `ticker`, `rating` (Portfolio Manager), `action`
(buy/sell/hold), `proposed_target_weight`, `target_weight`, `current_weight`, `thesis`,
`evidence[]` (statement, `verified_fact` | `hypothesis`, source report, `as_of`),
`confidence`, `uncertainty`, `risks[]`, `time_horizon`, `decision_timestamp`,
`data_timestamps` (analysis date, quote time and source, research start/finish),
`model_info`, `status` (accepted/rejected), `rejection_reasons[]`, `notes[]`.

### Validation (`research/decision.py`): LLM text never becomes an order directly

1. Direction comes from TradingAgents' typed rating: Buy/Overweight → buy,
   Hold → hold, Underweight/Sell → sell. An unparseable rating (`REVIEW`) is rejected.
2. The formalizer's structured proposal must name the same ticker and agree with that
   direction. Its evidence must include at least one verified fact, cite only reports that
   are non-empty in this run, and contain nothing dated after the analysis date.
3. Sizing: a buy below `min_confidence_for_buy` becomes hold. A buy must raise the weight.
   Underweight with no usable proposal halves the position. Sell → 0. Selling with no
   position → hold (no shorts).
4. Out-of-range sizes are **not** clipped here. The risk engine rejects them, so the violation
   is visible.

## Risk policy (`risk.py`, `config/default.yaml → risk`)

Evaluated per order on a pro-forma account that includes earlier orders in the same batch.
Any violation blocks the order, regardless of agent agreement:

| Rule | Default |
|---|---|
| Paper mode only (`mode: paper` is the only valid config value) | — |
| Kill switch inactive | — |
| Regular session only, not in the first/last N minutes | 5 / 5 min |
| Universe: US-listed common stock or ETF; leveraged/inverse denied | list in config |
| Whole shares, positive quantity, ≥ min notional | $100 |
| Quote age | ≤ 1,200 s |
| One order per ticker per side per session (duplicate guard) | 1 |
| No short sales (sell ≤ holding) | — |
| Buys: drawdown halt inactive | halt at 10 % from running peak |
| Buys: cash ≥ 0 after trade (no margin, no borrowing) | — |
| Buys: cash reserve after trade | ≥ 5 % of equity |
| Buys: position weight after trade | ≤ 10 % of equity |
| Buys: gross long exposure | ≤ 100 % |
| Buys: new purchases this session | ≤ 20 % of session-start equity |

Options, derivatives and leverage aren't reachable at all: orders are `market` orders for
`USStock` symbols on a paper token, and the universe gate fails closed when metadata is
missing.

## Execution assumptions (`execution/fill_model.py`)

| Assumption | Value | Rationale |
|---|---|---|
| Order type | Market, regular session only | No stale-price limit orders |
| Fill reference | QuantDinger's reference at submission (latest 1-min close); replays: next 1-min bar open after 2 s latency; daily backtests: that session's official open | Never the decision-time price |
| Reference sanity | Reject booking if venue reference deviates > 2 % from the pre-trade quote | Guards bad ticks / stale venue data |
| Slippage | 3 bps half-spread + 5 bps per 1 % of 20-day ADV, capped at 50 bps, adverse to side | Large-cap US spreads are typically 1–5 bps |
| Participation cap | 1 % of 20-day ADV; the excess is not filled → `partially_filled`, remainder cancelled | Avoids unrealistic size |
| Commission | $0 per share | Typical US retail broker |
| SEC Section 31 fee | $27.80 per $1M of sale proceeds | Verify the current SEC rate periodically |
| FINRA TAF | $0.000166 per share sold, max $8.30 | FINRA schedule |
| Dividends | Credited at the close mark for shares held before the ex-date open | Matches total-return benchmark |

## Idempotency and recovery

- `run_id = <account>:<session date>:<slot>`. Restarting a slot resumes it: decisions already
  stored are reused, so research isn't re-run, and completed steps are skipped. A completed
  slot returns `already_completed`.
- Order `idempotency_key = sha256(account | run_id | ticker | side)`. The ledger row is
  written as `PENDING_SUBMIT` *before* the venue call. QuantDinger's `Idempotency-Key` makes
  resubmission return the original fill. A crash between the venue fill and ledger booking
  is repaired by `recover()` at the next run, without a second venue order (tested).
- If an order is still pending after its session window ends, it's cancelled instead of being
  filled at a stale price.
- A file lock (`data/cycle.lock`) prevents concurrent cycles.

## Performance (`portfolio/performance.py`)

Equity curve from close marks. Cumulative return, max drawdown, Sharpe (daily, √252, configurable
risk-free rate), turnover, transaction costs, realized/unrealized P&L. SPY is compared over the
**same period** (from the account's inception close to the latest close) on a total-return
basis (dividends reinvested on ex-date), with price return shown too. Relative metrics come
from QuantDinger's `metrics.py`. LLM tokens and cost are tracked per call (`costs.py`) and
reported separately from investment performance. Unpriced models are counted as
"unpriced", never guessed.

## Backtests and replays

- `aipt backtest`: chronological daily loop through the same cycle, risk engine and ledger,
  in a separate `HISTORICAL_BACKTEST` ledger. Research sees data through the previous close
  only; orders fill at the session's official open + slippage/fees.
  Results are flagged `SURVIVORSHIP` (today's watchlist) and `LLM_LOOKAHEAD_RISK` unless
  `llm.knowledge_cutoff` precedes the period, or `SCRIPTED` for the offline rule.
- `aipt replay`: one session with real 1-minute bars, in a `REPLAY_DEMO` ledger.
- Live-forward paper results are labelled `LIVE_FORWARD_PAPER`. The three never share a ledger.

## Model configuration (`config/default.yaml → llm`)

`provider`, `quick_model`, `deep_model`, optional `quick_provider` / `deep_provider`, and a
`formalizer_provider` / `formalizer_model`. Anthropic is supported natively by TradingAgents.
Keys come from the environment only (`ANTHROPIC_API_KEY`, …). `--research scripted` runs the
real graph with a deterministic non-AI model for offline testing; its output is labelled
`SCRIPTED (not AI research)`.
