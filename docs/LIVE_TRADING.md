# Switching to a real brokerage account

aipt ships paper-only. Going live is a deliberate code change, not a config flag, and every
safeguard in this repository stays in force on a live account. This guide lists exactly what
to change, where, and which protections must remain.

## 1. Before you switch

- Run the live-forward paper account with **AI research** (`aipt run-cycle`, not
  `--research scripted`) across enough sessions to see buys, sells, rejections and at least
  one recovery. `aipt reconcile` must be clean on every one of them.
- Fill in `llm.prices` so the daily LLM budget is enforced on real spend.
- Decide the capital you'll fund the account with, and start with a small amount.

## 2. Choose a broker venue

QuantDinger's Agent Gateway can't route US-equity orders to a broker: its live path
(`vendor/QuantDinger/backend_api_python/app/routes/agent_v1/quick_trade.py`) accepts crypto
exchange credentials only and rejects everything else with *"Quick Trade currently supports
crypto exchange API keys only."* For US stocks and ETFs, add a broker venue next to the
existing ones in `aipt/execution/venues.py`.

[Alpaca](https://docs.alpaca.markets/) is the simplest fit: the same REST API serves paper
(`https://paper-api.alpaca.markets`) and live (`https://api.alpaca.markets`), so you can run
the new venue against Alpaca paper first and change only the base URL to go live.

| aipt venue contract (`PaperVenue` protocol) | Alpaca equivalent |
|---|---|
| `submit(idempotency_key, ticker, side, quantity)` | `POST /v2/orders` with `client_order_id = idempotency_key` (Alpaca rejects a reused `client_order_id`) |
| replay after a crash | `GET /v2/orders:by_client_order_id?client_order_id=...` before resubmitting |
| `VenueOrderResult.reference_price`, `filled_quantity` | the order's `filled_avg_price` and `filled_qty`; poll `GET /v2/orders/{id}` until `filled`, `partially_filled` past your timeout, `canceled` or `rejected` |
| `list_orders()` | `GET /v2/orders?status=all` |
| `emergency_stop()` | `DELETE /v2/orders` (cancel all open orders) |

Read the keys from the environment only (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`), add them
to `Secrets` in `aipt/config.py`, and keep them out of `repr` and logs like the QuantDinger
token (`aipt/logging_setup.py` redacts by pattern; add the Alpaca key prefix there).

## 3. Code changes

1. **Config (`aipt/config.py`).** Change `mode: Literal["paper"]` to
   `Literal["paper", "live"]`, and add `"alpaca"` to `ExecutionConfig.venue`. Make
   `mode: live` valid only when `execution.venue` is a broker venue, and require an
   environment confirmation as well, for example `AIPT_LIVE_CONFIRM=<account id>`, so a
   copied YAML file can't go live on its own.
2. **Venue (`aipt/execution/venues.py`).** Implement `AlpacaVenue` as in the table above.
   On startup, call `GET /v2/account` and refuse to run if the account's paper/live status
   doesn't match `mode` (Alpaca paper account numbers start with `PA`), if it's restricted
   from trading, or if it isn't a cash account and you intend to stay unlevered.
3. **Wiring (`aipt/system.py → build_live`).** Construct `AlpacaVenue` when
   `execution.venue == "alpaca"`; keep `QuantDingerPaperVenue.verify_paper_only()` as is.
4. **Adapter (`aipt/execution/adapter.py`).** Allow `mode == "live"` only with a broker venue.
   For live fills, book the broker's actual average price, quantity and fees instead of
   applying `FillModel` slippage to the reference price; keep the model for paper, replay
   and backtest.
5. **Reconciliation (`aipt/portfolio/reconcile.py`).** Add a broker check: ledger cash and
   positions against `GET /v2/account` and `GET /v2/positions`. Run it before every cycle
   and engage the kill switch on any mismatch.
6. **Ledger.** Use a new `account.id` for the live account and set `initial_capital` to the
   funded cash, so live history never mixes with paper.
7. **Tests.** Keep every test in `tests/test_paper_only.py`. Add tests that live mode is
   refused without the confirmation variable, refused with a paper venue, refused when the
   broker account type doesn't match `mode`, and that a crash between submit and booking
   resolves through `client_order_id` without a second order.

## 4. Safeguards that stay on

| Safeguard | Where | Default |
|---|---|---|
| Max position size | `risk.max_position_weight` | 10 % of equity |
| No leverage, long only | `risk.max_gross_exposure` (validator caps it at 1.0) | 100 % |
| Cash reserve after every buy | `risk.min_cash_reserve` | 5 % |
| Daily buy turnover | `risk.max_daily_buy_turnover` | 20 % of equity |
| Drawdown halt (buys blocked until reviewed) | `risk.drawdown_halt` | 10 % from peak |
| One order per ticker per session, idempotent keys | `risk.max_orders_per_ticker_per_session`, `adapter.idempotency_key` | 1 |
| Stale quote and price-deviation rejection | `risk.max_quote_age_seconds`, `execution.max_reference_price_deviation` | 20 min, 2 % |
| No trading near the open/close | `risk.session_open_buffer_minutes`, `session_close_buffer_minutes` | 5 min each |
| Leveraged/inverse products denied | `risk.denied_tickers`, `denied_name_patterns` | see config |
| LLM output validation, minimum buy confidence | `aipt/research/decision.py`, `allocation.min_confidence_for_buy` | 0.55 |
| Kill switch, checked before research, each order and mid-batch | `aipt kill` / `aipt resume` | manual |
| Daily LLM budget | `research.daily_llm_budget_usd` | $25 |

Suggested starting overrides for a new live account, in a separate file passed with
`--config`:

```yaml
mode: live
execution: {venue: alpaca}
risk:
  max_position_weight: 0.05
  max_daily_buy_turnover: 0.10
  drawdown_halt: 0.05
```

## 5. Go-live checklist

- [ ] The new venue passes the full test suite against Alpaca **paper**.
- [ ] Several sessions on Alpaca paper with AI research, reconciliation clean every day.
- [ ] Live keys in `.env` only (`chmod 600`), never in YAML or the repository.
- [ ] `aipt kill --reason test` then `aipt resume` exercised on the live configuration.
- [ ] You watch the first live sessions as they run and check `aipt reconcile` after each.

## Crypto through QuantDinger

If you adapt aipt to crypto instead, QuantDinger's own live gate applies on top of the
safeguards above: a token with `paper_only=false`, both `LIVE_TRADING_ENABLED=true` and
`AGENT_LIVE_TRADING_ENABLED=true` on the QuantDinger server, a `credential_id` in each order,
and the token's `max_order_notional` / `max_daily_notional` caps. aipt's universe, calendar
and cost model are US-equity specific and would need the same rework.
