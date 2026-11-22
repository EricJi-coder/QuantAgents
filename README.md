# aipt: AI multi-agent trading system

An autonomous research and execution system for US equities and ETFs:
[TradingAgents](https://github.com/TauricResearch/TradingAgents) runs the multi-agent LLM
research (analysts, bull/bear debate, trader, risk team, portfolio manager), a risk engine
enforces hard portfolio limits, [QuantDinger](https://github.com/OpenByteInc/QuantDinger)'s
Agent Gateway executes the orders, and a local ledger keeps the books, reconciled against
the venue. Performance can be published to [SavvyTrader](https://savvytrader.com/) through a
manual-entry queue.

It ships in **paper mode** with a $100,000 simulated USD account. The config accepts only
`mode: paper`, the QuantDinger token must be `paper_only`, and any non-paper response trips
the kill switch. To run it on a real brokerage account, follow
[docs/LIVE_TRADING.md](docs/LIVE_TRADING.md).

```bash
git clone --recurse-submodules <this repo> && cd <repo>
python3.13 -m venv .venv && .venv/bin/pip install -e vendor/TradingAgents -e ".[dev]"
source .venv/bin/activate
scripts/quantdinger_local.sh start   # local QuantDinger (paper only) on 127.0.0.1:5055
aipt qd-check                        # verifies the token is paper_only
aipt init                            # $100,000 cash account (idempotent)
aipt run-cycle                       # one research → risk → order cycle (needs ANTHROPIC_API_KEY)
aipt scheduler                       # daily: 10:30 ET decision cycle, 16:10 ET close mark
aipt status | aipt reconcile | aipt dashboard --serve | aipt savvy queue
pytest                               # offline test suite
```

Full setup (QuantDinger backend, PostgreSQL, Redis, tokens) is in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

Without an LLM key, `--research scripted` runs the same TradingAgents graph on a
deterministic momentum rule, clearly labelled as not AI research.
`aipt replay --date YYYY-MM-DD` replays one past session on real minute bars, and
`aipt backtest --start … --end …` runs a chronological daily backtest. Both use separate,
labelled ledgers.

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Data flow, state ownership, decision schema, risk policy, execution/cost assumptions |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Setup, scheduling, kill switch, drawdown halt, recovery, files, Docker |
| [docs/LIVE_TRADING.md](docs/LIVE_TRADING.md) | Switching to a real brokerage account with all safeguards kept |
| [docs/SAVVYTRADER.md](docs/SAVVYTRADER.md) | What SavvyTrader permits and the manual publishing workflow |
| [docs/STATUS.md](docs/STATUS.md) | Implemented / tested / untested / unavailable, with evidence |
| [docs/AUDIT.md](docs/AUDIT.md) | Audit of both upstream repositories: what is reused, gaps, risks |

Layout: `aipt/` (application), `config/default.yaml` (all limits and settings),
`vendor/` (upstream projects as pinned git submodules, unmodified),
`scripts/quantdinger_local.sh`, `tests/`, `data/` (ledgers, logs; git-ignored),
`reports/` (dashboard, run reports, SavvyTrader files; git-ignored).

Licensed under Apache-2.0. Not financial advice.
