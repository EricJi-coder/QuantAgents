"""Self-contained HTML dashboard (no external assets), regenerated after each cycle."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from aipt.ledger import DRAWDOWN_HALT, KILL_SWITCH
from aipt.models import AccountSnapshot, utcnow


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2%}"


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _svg_chart(series: dict[str, list[dict[str, Any]]], width: int = 860, height: int = 260) -> str:
    points = [p for s in series.values() for p in s]
    if len(points) < 2:
        return "<p class='muted'>The equity chart appears after two or more daily marks.</p>"
    dates = sorted({p["date"] for p in points})
    lo, hi = min(p["value"] for p in points), max(p["value"] for p in points)
    span = (hi - lo) or 1.0
    colors = ["#2563eb", "#9ca3af"]
    paths = []
    for (name, s), color in zip(series.items(), colors, strict=False):
        coords = [(40 + (width - 60) * dates.index(p["date"]) / max(len(dates) - 1, 1),
                   10 + (height - 40) * (1 - (p["value"] - lo) / span)) for p in s]
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
        paths.append(f"<path d='{d}' fill='none' stroke='{color}' stroke-width='2'/>"
                     f"<text x='{width - 200}' y='{20 + 16 * len(paths)}' fill='{color}' font-size='12'>"
                     f"{html.escape(name)}</text>")
    labels = (f"<text x='0' y='16' font-size='11' fill='#6b7280'>{_usd(hi)}</text>"
              f"<text x='0' y='{height - 26}' font-size='11' fill='#6b7280'>{_usd(lo)}</text>"
              f"<text x='40' y='{height - 6}' font-size='11' fill='#6b7280'>{dates[0]}</text>"
              f"<text x='{width - 90}' y='{height - 6}' font-size='11' fill='#6b7280'>{dates[-1]}</text>")
    return f"<svg width='{width}' height='{height}' role='img'>{''.join(paths)}{labels}</svg>"


def write_dashboard(system, perf: dict[str, Any], snap: AccountSnapshot) -> Path:
    ledger = system.ledger
    account = ledger.account()
    usage = ledger.llm_usage_summary()
    kill = ledger.control(KILL_SWITCH)
    halt = ledger.control(DRAWDOWN_HALT)
    positions_rows = []
    unrealized_total = 0.0
    for t, p in snap.positions.items():
        q = snap.prices[t]
        value = p.quantity * q.price
        upnl = p.quantity * (q.price - p.average_cost)
        unrealized_total += upnl
        positions_rows.append(
            f"<tr><td>{t}</td><td>{p.quantity:g}</td><td>{_usd(p.average_cost)}</td><td>{_usd(q.price)}"
            f"<br><span class='muted'>{q.timestamp:%Y-%m-%d %H:%M}Z</span></td><td>{_usd(value)}</td>"
            f"<td>{value / snap.equity:.2%}</td><td>{_usd(upnl)}</td></tr>")
    fills = ledger.fills()[-50:][::-1]
    trade_rows = "".join(
        f"<tr><td>{f['ts'][:19]}</td><td>{f['side'].upper()}</td><td>{f['ticker']}</td><td>{f['quantity']:g}</td>"
        f"<td>{_usd(f['reference_price'])}</td><td>{_usd(f['fill_price'])}</td><td>{f['slippage_bps']:.1f}</td>"
        f"<td>{_usd(f['commission'] + f['fees'])}</td><td>{_usd(f['realized_pnl']) if f['side'] == 'sell' else ''}</td></tr>"
        for f in fills)
    rejected = [o for o in ledger.orders(limit=30) if o["status"] in ("rejected", "failed", "cancelled")]
    rejected_rows = "".join(
        f"<tr><td>{o['created_at'][:19]}</td><td>{o['side']}</td><td>{o['ticker']}</td><td>{o['quantity']:g}</td>"
        f"<td>{o['status']}</td><td>{html.escape('; '.join(v['message'] for v in json.loads(o['violations'] or '[]')) or (o['reason'] or ''))}</td></tr>"
        for o in rejected)
    decision_rows = "".join(
        f"<tr><td>{d.decision_timestamp:%Y-%m-%d %H:%M}</td><td>{d.ticker}</td><td>{d.rating.value}</td>"
        f"<td>{d.action.value}</td><td>{d.status.value}</td>"
        f"<td>{'' if d.target_weight is None else f'{d.target_weight:.1%}'}</td>"
        f"<td>{'' if d.confidence is None else f'{d.confidence:.0%}'}</td>"
        f"<td>{html.escape((d.thesis or '')[:240])}{'<br><b>Rejected:</b> ' + html.escape('; '.join(d.rejection_reasons)) if d.rejection_reasons else ''}</td></tr>"
        for d in ledger.decisions(limit=15))
    bench = perf.get("benchmark") or {}
    rel = (bench.get("relative") or {})
    start_date = (bench.get("curve") or [{"date": account["created_at"][:10]}])[0]["date"]
    chart = _svg_chart({"Portfolio": [{"date": start_date, "value": account["initial_capital"]}] +
                        [p for p in perf.get("curve", []) if p["date"] > start_date],
                        f"{bench.get('ticker', 'SPY')} total return (same capital)": bench.get("curve", [])})
    status = []
    if kill.get("active"):
        status.append(f"<div class='alert'>KILL SWITCH ACTIVE — {html.escape(kill.get('note') or '')}</div>")
    if halt.get("active"):
        status.append(f"<div class='alert'>DRAWDOWN HALT — new buys blocked pending manual review "
                      f"({html.escape(halt.get('note') or '')})</div>")
    cards = [("Total value", _usd(perf["equity"])), ("Cash", _usd(perf["cash"])),
             ("Cumulative return", _pct(perf["cumulative_return"])),
             (f"{bench.get('ticker', 'SPY')} total return", _pct(bench.get("total_return"))),
             ("Max drawdown", f"{perf['max_drawdown']:.2%}"),
             ("Sharpe", "n/a" if perf["sharpe"] is None else f"{perf['sharpe']:.2f}"),
             ("Unrealized P&L", _usd(unrealized_total)), ("Realized P&L", _usd(perf["realized_pnl"])),
             ("Turnover (buys / sells)", f"{perf['turnover_buys']:.1%} / {perf['turnover_sells']:.1%}"),
             ("Transaction costs", _usd(perf["transaction_costs"]["total"])),
             ("Beta / alpha vs SPY", f"{rel.get('beta'):.2f} / {rel.get('alphaAnnualized'):+.2%}"
              if rel.get("beta") is not None and rel.get("alphaAnnualized") is not None else "n/a"),
             ("LLM tokens (in / out)", f"{usage['input_tokens'] or 0:,} / {usage['output_tokens'] or 0:,}"),
             ("LLM cost (priced)", _usd(usage["priced_cost_usd"]) +
              (f" + {usage['unpriced_calls']} unpriced calls" if usage["unpriced_calls"] else ""))]
    card_html = "".join(f"<div class='card'><div class='muted'>{k}</div><div class='v'>{v}</div></div>" for k, v in cards)
    page = f"""<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(account['name'])}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;margin:24px;color:#111827}}
.banner{{background:#fef3c7;border:1px solid #f59e0b;padding:10px 14px;border-radius:6px;margin-bottom:14px}}
.alert{{background:#fee2e2;border:1px solid #ef4444;padding:10px 14px;border-radius:6px;margin-bottom:10px;font-weight:600}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;margin:14px 0}}
.card{{border:1px solid #e5e7eb;border-radius:6px;padding:10px}} .v{{font-size:18px;font-weight:600;margin-top:4px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 22px;font-size:13px}} td,th{{border-bottom:1px solid #e5e7eb;padding:5px;text-align:left;vertical-align:top}}
.muted{{color:#6b7280;font-size:12px}}</style></head><body>
<div class='banner'><b>PAPER TRADING ONLY — SIMULATED, AI-MANAGED.</b> No real money. Ledger label:
<code>{account['label']}</code>. {'Live-forward paper results.' if account['label'] == 'LIVE_FORWARD_PAPER' else 'Not live-forward results.'}</div>
<h1>{html.escape(account['name'])}</h1>
<div class='muted'>Updated {utcnow():%Y-%m-%d %H:%M:%S} UTC · inception {account['created_at'][:10]} ·
initial capital {_usd(account['initial_capital'])} {account['base_currency']}</div>
{''.join(status)}<div class='cards'>{card_html}</div>
<h2>Equity vs benchmark</h2>{chart}
<p class='muted'>Benchmark: {html.escape(str(bench.get('period') or bench.get('error') or 'n/a'))}. Portfolio dividends are credited on ex-date, so the comparison uses SPY total return.</p>
<h2>Open positions</h2><table><tr><th>Ticker</th><th>Shares</th><th>Avg cost</th><th>Last</th><th>Value</th><th>Weight</th><th>Unrealized</th></tr>
{''.join(positions_rows) or '<tr><td colspan=7 class=muted>100% cash</td></tr>'}</table>
<h2>Latest AI decisions</h2><table><tr><th>Time (UTC)</th><th>Ticker</th><th>Rating</th><th>Action</th><th>Status</th><th>Target</th><th>Conf.</th><th>Thesis</th></tr>{decision_rows}</table>
<h2>Trade history (simulated fills)</h2><table><tr><th>Time (UTC)</th><th>Side</th><th>Ticker</th><th>Qty</th><th>Reference</th><th>Fill</th><th>Slip bps</th><th>Fees</th><th>Realized</th></tr>{trade_rows}</table>
<h2>Rejected / failed orders</h2><table><tr><th>Time</th><th>Side</th><th>Ticker</th><th>Qty</th><th>Status</th><th>Reason</th></tr>{rejected_rows}</table>
</body></html>"""
    out = Path(system.settings.paths.reports_dir) / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out
