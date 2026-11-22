# Status (2026-11-22)

## Implemented and tested

Offline suite: `pytest`, 38 tests passing, no network, LLM or QuantDinger needed.
QuantDinger integration: `pytest -m quantdinger`, 3 tests passing against the running local gateway.

| Requirement | Evidence |
|---|---|
| Starts with exactly $100,000 cash, 100 % cash, paper mode | `test_account_starts_with_exactly_100k_cash`; live account created with `aipt init` |
| Valid recommendation → validation → order → fill → cash/positions/equity | `test_full_cycle_buy_updates_cash_positions_and_equity`, `test_sell_closes_position_and_books_realized_pnl_with_fees` |
| Oversized position rejected even with unanimous agents | `test_oversized_position_is_rejected_even_with_unanimous_agents` |
| Duplicate orders prevented (retry, same-session, venue replay) | `test_retrying_a_completed_slot_does_not_duplicate_orders`, `test_second_buy_same_ticker_same_session_is_rejected_as_duplicate`, `test_venue_resubmission_with_same_key_returns_original_fill` |
| Recovery from interruption | `test_recovers_from_crash_between_venue_fill_and_ledger_booking`, `test_venue_outage_leaves_order_pending_then_retry_fills_it`, `test_pending_order_after_session_is_cancelled_not_filled_stale` |
| SavvyTrader-compatible publication record + discrepancy tracking | `test_produces_savvytrader_publication_record_and_tracks_discrepancy` |
| Market hours, universe, cash reserve, turnover, drawdown halt, kill switch, partial fills, dividends | the corresponding tests in `tests/test_paper_cycle.py` |
| LLM output validation (missing structure, contradiction, no verified fact, look-ahead evidence, wrong ticker, no rating, low confidence) | `test_invalid_llm_output_is_rejected_and_never_executed[...]`, `test_low_confidence_buy_becomes_hold` |
| Real trading disabled | `tests/test_paper_only.py`: config rejects non-paper modes and leverage; non-paper token, non-paper response and wrong scope/market refused; no `credential_id` sent |
| TradingAgents graph → ledger, end to end | `test_scripted_tradingagents_cycle_books_fills_and_reconciles` (real graph, scripted model) |
| Chronological backtest, fills at that session's open, labelled | `test_backtest_fills_at_each_sessions_open_and_is_labelled` |
| QuantDinger token is paper-only, quotes timestamped | `tests/test_quantdinger_integration.py` |

## Demonstrated on real market data

- **Replay of 2026-11-20** (`aipt replay --date 2026-11-20 --tickers COST,WMT,LLY,PG,BRK-B,KO,XLP,AAPL`,
  real Yahoo minute bars, scripted research through the real TradingAgents graph, `REPLAY_DEMO` ledger):
  - Initial: cash $100,000.00, no positions.
  - 8 decisions: 4 Overweight → buy 5 %, 4 Hold. Buys of COST 4, WMT 38, LLY 3 and BRK-B 8
    filled at the next minute's open + 3 bps.
  - Final at the close: cash $81,892.26, positions $17,885.08, equity $99,777.34 (−0.22 %;
    SPY −3.64 % that day). Reconciliation OK.
- **Backtest 2026-10-01 → 2026-11-20** (6 tickers, scripted, `HISTORICAL_BACKTEST`): 17 orders,
  all filled on their own session's open, none failed, reconciliation clean. Equity $101,837
  (+1.84 %) vs SPY total return −9.55 % over the same period (SPY verified against Yahoo:
  762.63 → 689.81). Flagged SURVIVORSHIP and SCRIPTED, since it ran on the rule-based
  research mode.
- **Live-forward account** (`LIVE_FORWARD_PAPER`): `aipt init` creates it with $100,000.00
  cash against the local QuantDinger gateway, and `aipt reconcile` passes on it.

## Implemented, not yet exercised

| Item | Why | How to exercise |
|---|---|---|
| AI (LLM) research via TradingAgents + formalizer | Needs an LLM API key, so the offline suite uses the scripted model | Add `ANTHROPIC_API_KEY` to `.env`, then `aipt run-cycle` |
| Paper order through QuantDinger's `/quick-trade/orders` during market hours | Read-only gateway calls are tested; the order path is tested against an emulation of the same contract | `aipt run-cycle` on a trading day after 09:35 ET, then `aipt reconcile` |
| LLM cost pricing | `llm.prices` is empty by design (no guessed prices); tokens are still counted | Fill in from your provider's price list |
| Docker deployment | Not covered by the test suite | `docker compose up -d --build` |
| Long-running scheduler | Logic is unit-sized and deterministic, but it hasn't run across a live session | `aipt scheduler` (or a LaunchAgent) |

## Manual / unavailable

- **SavvyTrader publishing is manual.** There's no API or import, and automation is forbidden
  by its Terms (docs/SAVVYTRADER.md). aipt generates the queue, comments and report, and you
  record SavvyTrader's own prices with `aipt savvy confirm`.
- **Per-agent model routing**: TradingAgents exposes two tiers (quick/deep) plus our formalizer.
  Routing per individual agent would require forking TradingAgents' `GraphSetup`.
- **Survivorship-free backtests** need a point-in-time constituent/delisting dataset, which isn't
  available here. Backtests are flagged instead.

## Before you run it

1. **LLM API key** (for AI research): create one at https://console.anthropic.com/settings/keys,
   add `ANTHROPIC_API_KEY=...` to `.env`, and set the models in `config/default.yaml → llm`
   to ones your account can use.
2. **SavvyTrader**: create a portfolio named *AI Multi-Agent Research Portfolio (SIMULATED)* with
   $100,000 virtual cash and the disclaimer in its description, then enter queued trades as they
   appear.
