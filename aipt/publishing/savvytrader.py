"""SavvyTrader publication: manual-entry queue and publication-ready report.

What SavvyTrader officially offers (checked 2026-11-22, docs/SAVVYTRADER.md):
- No public API, no third-party integration program, no brokerage linking, and no import of
  past trades: its FAQ says trades are executed virtually at SavvyTrader's own real-time price
  at the moment they are entered, during market hours.
- Its Terms of Service forbid "any robot, spider, or other automatic device, process, or means
  to access the Service", and robots.txt disallows api.savvytrader.com.

So this module never contacts SavvyTrader. It produces a queue of trades for a person to enter
in the SavvyTrader app (with suggested comments), records what SavvyTrader actually filled when
the operator confirms an entry, and reports any difference. The local ledger stays the
authoritative record; SavvyTrader is a public tracking destination.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aipt.config import Settings
from aipt.ledger import Ledger
from aipt.models import Quote, utcnow


def _comment(decision: dict[str, Any] | None, fill: dict[str, Any], disclaimer_tag: str) -> str:
    thesis = ""
    if decision:
        thesis = (decision.get("thesis") or "").strip().replace("\n", " ")
        if len(thesis) > 380:
            thesis = thesis[:377] + "..."
        conf = decision.get("confidence")
        thesis = (f"{decision.get('rating')} (confidence {conf:.0%}). {thesis}" if conf is not None
                  else f"{decision.get('rating')}. {thesis}")
    return (f"[{disclaimer_tag}] {thesis} Local sim fill {fill['quantity']:g} @ ${fill['fill_price']:.2f} "
            f"({fill['ts'][:16]}Z). Decision {fill.get('decision_id') or 'n/a'}.").strip()


class SavvyTraderPublisher:
    TAG = "SIMULATED AI PAPER PORTFOLIO"

    def __init__(self, settings: Settings, ledger: Ledger):
        self.settings = settings
        self.ledger = ledger
        self.out_dir = Path(settings.paths.reports_dir) / "savvytrader"

    def sync_queue(self, now: datetime | None = None) -> int:
        """Queue every booked fill that is not yet queued. Returns how many were added."""
        now = now or utcnow()
        queued = {e["fill_id"] for e in self.ledger.publication_entries()}
        decisions = {d.decision_id: d.model_dump(mode="json") for d in self.ledger.decisions(limit=100000)}
        orders = {o["order_id"]: o for o in self.ledger.orders(limit=100000)}
        added = 0
        for f in self.ledger.fills():
            if f["fill_id"] in queued:
                continue
            decision_id = orders.get(f["order_id"], {}).get("decision_id")
            f = {**f, "decision_id": decision_id}
            self.ledger.enqueue_publication(f["fill_id"], f["ticker"], f["side"], f["quantity"], f["fill_price"],
                                            f["ts"], _comment(decisions.get(decision_id), f, self.TAG), now)
            added += 1
        return added

    def confirm(self, entry_id: int, savvy_price: float, savvy_quantity: float | None, savvy_ts: str | None,
                entered_by: str, note: str = "") -> dict[str, Any]:
        """Record SavvyTrader's own virtual execution for a queued entry (both records are kept)."""
        return self.ledger.confirm_publication(entry_id, status="entered", savvy_price=savvy_price,
                                               savvy_quantity=savvy_quantity, savvy_ts=savvy_ts,
                                               entered_by=entered_by, note=note, now=utcnow())

    def skip(self, entry_id: int, entered_by: str, note: str) -> dict[str, Any]:
        return self.ledger.confirm_publication(entry_id, status="skipped", savvy_price=None, savvy_quantity=None,
                                               savvy_ts=None, entered_by=entered_by, note=note, now=utcnow())

    def discrepancies(self) -> list[dict[str, Any]]:
        out = []
        for e in self.ledger.publication_entries():
            row = {"entry_id": e["entry_id"], "ticker": e["ticker"], "side": e["side"], "status": e["status"],
                   "local_quantity": e["quantity"], "local_price": e["local_fill_price"],
                   "savvy_quantity": e["savvy_quantity"], "savvy_price": e["savvy_price"]}
            if e["status"] == "entered" and e["savvy_price"]:
                diff = e["savvy_price"] / e["local_fill_price"] - 1
                row["price_diff_bps"] = round(diff * 10_000, 1)
                adverse = diff if e["side"] == "buy" else -diff
                row["cost_vs_local_usd"] = round(adverse * e["local_fill_price"] * (e["savvy_quantity"] or e["quantity"]), 2)
                if e["savvy_quantity"] is not None and abs(e["savvy_quantity"] - e["quantity"]) > 1e-9:
                    row["quantity_mismatch"] = True
            out.append(row)
        return out

    def publication_record(self, perf: dict[str, Any], prices: dict[str, Quote]) -> dict[str, Any]:
        account = self.ledger.account()
        positions = self.ledger.positions()
        equity = perf["equity"]
        holdings = []
        for t, p in positions.items():
            price = prices[t].price if t in prices else None
            value = p.quantity * price if price else None
            holdings.append({"ticker": t, "shares": p.quantity, "average_cost": round(p.average_cost, 4),
                             "last_price": price, "price_time": prices[t].timestamp.isoformat() if t in prices else None,
                             "market_value": round(value, 2) if value else None,
                             "allocation": round(value / equity, 4) if value and equity else None,
                             "unrealized_pnl": round(p.quantity * (price - p.average_cost), 2) if price else None})
        holdings.sort(key=lambda h: -(h["market_value"] or 0))
        decisions = [d.model_dump(mode="json") for d in self.ledger.decisions(limit=20)]
        return {
            "record_type": "savvytrader_publication",
            "generated_at": utcnow().isoformat(),
            "simulated": True,
            "ai_managed": True,
            "disclaimer": self.settings.publishing.savvytrader.disclaimer.strip(),
            "portfolio_name": self.settings.publishing.savvytrader.portfolio_name,
            "account": {"name": account["name"], "base_currency": account["base_currency"],
                        "initial_capital": account["initial_capital"], "inception": account["created_at"],
                        "label": account["label"]},
            "current_value": round(equity, 2),
            "cash": round(perf["cash"], 2),
            "cash_allocation": round(perf["cash"] / equity, 4) if equity else None,
            "performance_since_inception": {
                "cumulative_return": perf["cumulative_return"], "max_drawdown": perf["max_drawdown"],
                "sharpe": perf["sharpe"], "benchmark": {k: v for k, v in (perf.get("benchmark") or {}).items()
                                                        if k in ("ticker", "period", "total_return", "price_return",
                                                                 "excess_total_return", "error")}},
            "holdings": holdings,
            "trade_history": [{"entry_id": e["entry_id"], "ticker": e["ticker"], "side": e["side"],
                               "quantity": e["quantity"], "local_sim_fill_price": e["local_fill_price"],
                               "local_sim_fill_time": e["local_fill_ts"], "savvytrader_status": e["status"],
                               "savvytrader_price": e["savvy_price"], "comment": e["comment"]}
                              for e in self.ledger.publication_entries()],
            "latest_commentary": [{"ticker": d["ticker"], "rating": d["rating"], "action": d["action"],
                                   "status": d["status"], "thesis": d["thesis"][:600], "confidence": d["confidence"],
                                   "decided_at": d["decision_timestamp"]} for d in decisions],
            "price_discrepancies": self.discrepancies(),
        }

    def write(self, perf: dict[str, Any], prices: dict[str, Quote]) -> dict[str, Path]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        record = self.publication_record(perf, prices)
        paths = {"json": self.out_dir / "publication_record.json", "markdown": self.out_dir / "publication_report.md",
                 "queue_csv": self.out_dir / "trade_entry_queue.csv"}
        paths["json"].write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        with paths["queue_csv"].open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["entry_id", "status", "action", "ticker", "shares", "local_sim_fill_price",
                        "local_sim_fill_time_utc", "comment_to_paste"])
            for e in self.ledger.publication_entries():
                w.writerow([e["entry_id"], e["status"], e["side"].upper(), e["ticker"], f"{e['quantity']:g}",
                            f"{e['local_fill_price']:.4f}", e["local_fill_ts"], e["comment"]])
        paths["markdown"].write_text(self._markdown(record), encoding="utf-8")
        return paths

    def _markdown(self, r: dict[str, Any]) -> str:
        perf = r["performance_since_inception"]
        bench = perf.get("benchmark") or {}
        lines = [
            f"# {r['portfolio_name']}", "",
            f"> **{r['disclaimer']}**", "",
            f"Generated {r['generated_at']} · inception {r['account']['inception'][:10]} · "
            f"initial capital ${r['account']['initial_capital']:,.2f} · ledger label `{r['account']['label']}`", "",
            "## Summary", "",
            f"- Current value: **${r['current_value']:,.2f}** (cash ${r['cash']:,.2f})",
            f"- Return since inception: **{perf['cumulative_return']:+.2%}**",
            f"- Max drawdown: {perf['max_drawdown']:.2%}",
            f"- Sharpe: {perf['sharpe']:.2f}" if perf.get("sharpe") is not None else "- Sharpe: n/a (needs 2+ daily returns)",
        ]
        if bench.get("total_return") is not None:
            lines.append(f"- {bench['ticker']} total return, same period ({bench['period']}): "
                         f"{bench['total_return']:+.2%} (excess {bench['excess_total_return']:+.2%})")
        lines += ["", "## Holdings", "", "| Ticker | Shares | Avg cost | Last | Value | Allocation | Unrealized |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for h in r["holdings"]:
            lines.append(f"| {h['ticker']} | {h['shares']:g} | ${h['average_cost']:,.2f} | "
                         f"${(h['last_price'] or 0):,.2f} | ${(h['market_value'] or 0):,.2f} | "
                         f"{(h['allocation'] or 0):.2%} | ${(h['unrealized_pnl'] or 0):,.2f} |")
        lines.append(f"| CASH | | | | ${r['cash']:,.2f} | {(r['cash_allocation'] or 0):.2%} | |")
        lines += ["", "## Trade entry queue (enter manually in SavvyTrader during market hours)", "",
                  "SavvyTrader fills each trade at its own real-time price when you enter it. After entering, record "
                  "SavvyTrader's price with `aipt savvy confirm <entry_id> --price <price>` so both records are kept.",
                  "", "| # | Status | Action | Ticker | Shares | Local sim fill | SavvyTrader price |",
                  "|---:|---|---|---|---:|---:|---:|"]
        for t in r["trade_history"]:
            sp = f"${t['savvytrader_price']:,.2f}" if t["savvytrader_price"] else "—"
            lines.append(f"| {t['entry_id']} | {t['savvytrader_status']} | {t['side'].upper()} | {t['ticker']} | "
                         f"{t['quantity']:g} | ${t['local_sim_fill_price']:,.2f} @ {t['local_sim_fill_time'][:16]} | {sp} |")
        lines += ["", "## Latest AI commentary", ""]
        for c in r["latest_commentary"][:10]:
            conf = f", confidence {c['confidence']:.0%}" if c["confidence"] is not None else ""
            lines.append(f"- **{c['ticker']}** — {c['rating']} → {c['action']} ({c['status']}{conf}): {c['thesis']}")
        disc = [d for d in r["price_discrepancies"] if d.get("price_diff_bps") is not None]
        if disc:
            lines += ["", "## SavvyTrader vs local simulated fills", "",
                      "| # | Ticker | Side | Local | SavvyTrader | Diff (bps) | Cost vs local |", "|---:|---|---|---:|---:|---:|---:|"]
            for d in disc:
                lines.append(f"| {d['entry_id']} | {d['ticker']} | {d['side']} | ${d['local_price']:,.2f} | "
                             f"${d['savvy_price']:,.2f} | {d['price_diff_bps']:+.1f} | ${d['cost_vs_local_usd']:,.2f} |")
        return "\n".join(lines) + "\n"
