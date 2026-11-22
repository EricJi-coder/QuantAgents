# SavvyTrader publishing

## What SavvyTrader officially supports (checked 2026-11-22)

| Mechanism | Available? | Source |
|---|---|---|
| Public / developer API | **No.** No documentation; `robots.txt` disallows `api.savvytrader.com` (the web app's private backend) | savvytrader.com/robots.txt |
| Authorized third-party integration | **No** program offered | site, FAQ |
| Brokerage linking (Plaid etc.) | **No.** The FAQ explains why they don't offer it | savvytrader.com/faq |
| Import of past trades / prices | **No.** "Savvy Trader uses real-time market pricing to virtually execute each trade … during open market hours" | savvytrader.com/faq |
| Manual trade entry with a comment per trade | **Yes** ("less than 30 seconds" per trade) | savvytrader.com/faq |
| Automated access | **Forbidden.** The Terms prohibit "any robot, spider, or other automatic device, process, or means to access the Service" | savvytrader.com/terms |

So the strongest permitted workflow is **manual entry from a generated queue**. aipt never
contacts SavvyTrader, stores no SavvyTrader credentials, and doesn't automate the website.

## Workflow

1. Every booked paper fill is added to `publication_queue` with a ready-to-paste comment:
   `[SIMULATED AI PAPER PORTFOLIO] <rating> (confidence ..). <thesis> Local sim fill <qty> @ <price> (<time>Z). Decision <id>.`
2. After each cycle, and after `aipt report`, these files are written:
   - `reports/savvytrader/trade_entry_queue.csv` — what to enter
   - `reports/savvytrader/publication_report.md` — holdings, allocations, performance since
     inception vs SPY, trade history, AI commentary, disclaimer
   - `reports/savvytrader/publication_record.json` — the same, machine-readable
     (`simulated: true`, `ai_managed: true`)
3. During market hours, enter each pending trade in your SavvyTrader portfolio (named
   *AI Multi-Agent Research Portfolio (SIMULATED)*; put the disclaimer from
   `config/default.yaml` in its description), pasting the comment.
4. Record SavvyTrader's own execution:
   `aipt savvy confirm <entry_id> --price <SavvyTrader price> --entered-by <you> [--quantity N]`
   or `aipt savvy skip <entry_id> --entered-by <you> --note "<why>"`.
5. `aipt savvy discrepancies` (and the report) show local simulated price vs SavvyTrader
   price per trade, in bps and dollars.

## Integrity rules

- SavvyTrader executes at **its** real-time price when you enter the trade, so its returns
  will differ from the local ledger. Both are kept, and the difference is reported. Local
  fills are never described as SavvyTrader executions.
- Initial setup: create the SavvyTrader portfolio with $100,000 virtual cash on the day the
  first trades are entered. Enter trades promptly after each cycle to minimise drift.
- The local ledger remains authoritative for performance reporting.
- If SavvyTrader later publishes an official API or import, add an adapter implementing
  `sync_queue`/`confirm` against it. Until then, the private backend stays off-limits.
