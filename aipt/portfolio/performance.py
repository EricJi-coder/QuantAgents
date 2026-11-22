"""Performance and benchmark evaluation.

Conventions:
- Equity curve: the last snapshot of each session (``close`` snapshots preferred).
- Returns: daily simple returns of equity. The account has no external cash flows after the
  initial deposit, so these are also time-weighted returns.
- Dividends are credited to cash on the ex-date for shares held before it, so the portfolio
  is compared with SPY *total return* (adjusted close); SPY price return is shown too.
- Sharpe: mean daily excess return / stdev * sqrt(252), with the configured annual risk-free
  rate de-annualised geometrically. Undefined with fewer than two returns.
- Turnover: traded notional / average equity, buys and sells separately.
- Benchmark-relative metrics (beta, alpha, information ratio, capture) and downside risk come
  from QuantDinger's ``app.services.backtest.metrics``.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import pandas as pd

from aipt import vendor
from aipt.ledger import Ledger
from aipt.market.calendar import USEquityCalendar

TRADING_DAYS = 252


def equity_curve(ledger: Ledger) -> pd.Series:
    snaps = ledger.snapshots()
    if not snaps:
        return pd.Series(dtype=float)
    frame = pd.DataFrame(snaps)
    frame["rank"] = (frame["kind"] == "close").astype(int)
    frame = frame.sort_values(["session_date", "rank", "ts"])
    last = frame.groupby("session_date").tail(1)
    return pd.Series(last["equity"].values, index=pd.to_datetime(last["session_date"]), dtype=float)


def max_drawdown(values: pd.Series, start_value: float) -> float:
    if values.empty:
        return 0.0
    series = pd.concat([pd.Series([start_value]), values.reset_index(drop=True)])
    peaks = series.cummax()
    return float(((peaks - series) / peaks).max())


def sharpe(returns: pd.Series, risk_free_annual: float) -> float | None:
    if len(returns) < 2:
        return None
    rf_daily = (1 + risk_free_annual) ** (1 / TRADING_DAYS) - 1
    excess = returns - rf_daily
    sd = float(excess.std(ddof=1))
    if sd == 0 or math.isnan(sd):
        return None
    return float(excess.mean() / sd * math.sqrt(TRADING_DAYS))


def _curve_points(series: pd.Series, calendar: USEquityCalendar) -> list[dict[str, Any]]:
    return [{"time": calendar.session_close(ts.date()).isoformat(), "value": float(v)} for ts, v in series.items()]


def benchmark_series(market_data, ticker: str, start: date, end: date) -> tuple[pd.Series, pd.Series]:
    bars = market_data.daily_bars(ticker, start, end)
    if bars is None or bars.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    idx = pd.to_datetime([ts.date() for ts in bars.index])
    return (pd.Series(bars["close"].values, index=idx, dtype=float),
            pd.Series(bars["adj_close"].values, index=idx, dtype=float))


def compute(ledger: Ledger, market_data=None, benchmark: str = "SPY", risk_free: float = 0.0,
            calendar: USEquityCalendar | None = None) -> dict[str, Any]:
    calendar = calendar or USEquityCalendar()
    account = ledger.account()
    initial = float(account["initial_capital"])
    curve = equity_curve(ledger)
    fills = ledger.fills()
    snaps = ledger.snapshots()
    latest = snaps[-1] if snaps else None
    equity_now = float(latest["equity"]) if latest else initial

    returns = curve.pct_change().dropna() if len(curve) > 1 else pd.Series(dtype=float)
    if len(curve):
        first_return = curve.iloc[0] / initial - 1
        returns = pd.concat([pd.Series([first_return], index=[curve.index[0]]), returns])

    buys = sum(f["notional"] for f in fills if f["side"] == "buy")
    sells = sum(f["notional"] for f in fills if f["side"] == "sell")
    avg_equity = float(curve.mean()) if len(curve) else initial
    explicit_costs = sum(f["commission"] + f["fees"] for f in fills)
    slippage_cost = sum(abs(f["fill_price"] - f["reference_price"]) * f["quantity"] for f in fills)

    out: dict[str, Any] = {
        "label": account["label"],
        "inception": account["created_at"],
        "initial_capital": initial,
        "equity": equity_now,
        "cash": float(latest["cash"]) if latest else ledger.cash(),
        "market_value": float(latest["market_value"]) if latest else 0.0,
        "cumulative_return": equity_now / initial - 1,
        "max_drawdown": max_drawdown(pd.Series([s["equity"] for s in snaps], dtype=float), initial),
        "current_drawdown": float(latest["drawdown"]) if latest else 0.0,
        "sharpe": sharpe(returns, risk_free),
        "trading_days": int(len(curve)),
        "turnover_buys": buys / avg_equity if avg_equity else 0.0,
        "turnover_sells": sells / avg_equity if avg_equity else 0.0,
        "transaction_costs": {"commission_and_fees": explicit_costs, "slippage": slippage_cost,
                              "total": explicit_costs + slippage_cost},
        "realized_pnl": ledger.total_realized(),
        "dividends": ledger.total_dividends(),
        "trades": len(fills),
        "benchmark": None,
    }

    if market_data is not None and len(curve):
        start, end = curve.index[0].date(), curve.index[-1].date()
        # Benchmark starts from the last close before inception so both series cover the same days.
        base_day = calendar.previous_session(start)
        price, total = benchmark_series(market_data, benchmark, base_day, end)
        if len(price) >= 2 and price.index[0].date() <= base_day:
            price, total = price[price.index >= pd.Timestamp(base_day)], total[total.index >= pd.Timestamp(base_day)]
            bench: dict[str, Any] = {
                "ticker": benchmark,
                "period": f"{base_day} close to {end} close",
                "price_return": float(price.iloc[-1] / price.iloc[0] - 1),
                "total_return": float(total.iloc[-1] / total.iloc[0] - 1),
                "max_drawdown": max_drawdown(total.iloc[1:] / total.iloc[0] * initial, initial),
            }
            bench["excess_total_return"] = out["cumulative_return"] - bench["total_return"]
            port_curve = _curve_points(pd.concat([pd.Series([initial], index=[pd.Timestamp(base_day)]), curve]),
                                       calendar)
            bench_curve = _curve_points(total, calendar)
            metrics = vendor.backtest_metrics()
            try:
                bench["relative"] = metrics.calculate_benchmark_relative_metrics(
                    port_curve, bench_curve, benchmark=benchmark, frequency="1d",
                    annualization_factor=TRADING_DAYS, risk_free_rate=risk_free, market="USStock")
                bench["downside"] = metrics.calculate_downside_risk(returns, periods_per_year=TRADING_DAYS)
            except Exception as exc:  # metrics need history; report why instead of failing the report
                bench["relative_error"] = str(exc)
            bench["curve"] = [{"date": ts.date().isoformat(), "value": float(v / total.iloc[0] * initial)}
                              for ts, v in total.items()]
            out["benchmark"] = bench
        else:
            out["benchmark"] = {"ticker": benchmark, "error": "benchmark history unavailable for the period"}
    out["curve"] = [{"date": ts.date().isoformat(), "value": float(v)} for ts, v in curve.items()]
    return out
