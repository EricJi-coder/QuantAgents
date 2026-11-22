"""Command line: `aipt --help`. Every command operates in paper mode only."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from aipt.config import load_dotenv_file, load_settings
from aipt.logging_setup import setup_logging

app = typer.Typer(no_args_is_help=True, help="AI Multi-Agent Research Portfolio — PAPER TRADING ONLY.")
savvy = typer.Typer(no_args_is_help=True, help="SavvyTrader manual publication queue.")
app.add_typer(savvy, name="savvy")

ConfigOpt = Annotated[str | None, typer.Option("--config", "-c", help="YAML overriding config/default.yaml")]
ResearchOpt = Annotated[str, typer.Option(help="tradingagents (LLM; needs API key) | scripted (offline rule, not AI)")]
VenueOpt = Annotated[str | None, typer.Option(help="quantdinger | local (default: config)")]


def _print(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _settings(config: str | None):
    load_dotenv_file()
    settings = load_settings(config)
    setup_logging(Path(settings.paths.data_dir) / "logs")
    return settings


def _system(config: str | None, research: str = "tradingagents", venue: str | None = None,
            with_research: bool = False):
    from aipt.system import build_live

    return build_live(_settings(config), research=research, venue_kind=venue, with_research=with_research)


def _portfolio(system) -> dict[str, Any]:
    snap, _ = system.adapter.snapshot()
    return {"cash": round(snap.cash, 2), "market_value": round(snap.market_value, 2), "equity": round(snap.equity, 2),
            "positions": {t: {"shares": p.quantity, "avg_cost": round(p.average_cost, 4),
                              "last": snap.prices[t].price, "weight": round(snap.weight(t), 4)}
                          for t, p in snap.positions.items()}}


@app.command()
def init(config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Create the paper account with its configured starting cash (idempotent)."""
    system = _system(config, venue=venue)
    created = system.init_account()
    a = system.ledger.account()
    _print({"created": created, "account": a, "cash": system.ledger.cash()})


@app.command()
def status(config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Portfolio, controls, recent runs and pending publication entries."""
    from aipt.ledger import DRAWDOWN_HALT, KILL_SWITCH

    system = _system(config, venue=venue)
    _print({"mode": system.settings.mode, "venue": system.venue.name, "account": system.ledger.account(),
            "portfolio": _portfolio(system), "kill_switch": system.ledger.control(KILL_SWITCH),
            "drawdown_halt": system.ledger.control(DRAWDOWN_HALT),
            "recent_runs": [{k: r[k] for k in ("run_id", "status", "error", "started_at", "finished_at")}
                            for r in system.ledger.runs(10)],
            "pending_savvytrader_entries": len(system.ledger.publication_entries("pending")),
            "llm_usage": system.ledger.llm_usage_summary()})


@app.command("run-cycle")
def run_cycle(config: ConfigOpt = None, research: ResearchOpt = "tradingagents", venue: VenueOpt = None,
              slot: Annotated[str | None, typer.Option(help="Run slot name; reuse it to resume a run")] = None,
              tickers: Annotated[str | None, typer.Option(help="Comma-separated tickers (default: holdings + watchlist)")] = None,
              execute: Annotated[bool, typer.Option(help="Submit paper orders for accepted decisions")] = True) -> None:
    """Run one decision cycle now (manual run)."""
    system = _system(config, research=research, venue=venue, with_research=True)
    slot = slot or f"manual-{system.clock.now():%H%M%S}"
    result = system.run_cycle(slot, tickers=tickers.split(",") if tickers else None, execute=execute)
    _print({"run_id": result.run_id, "status": result.status, **result.detail, "portfolio": _portfolio(system)})
    raise typer.Exit(0 if result.status in ("completed", "already_completed") else 1)


@app.command()
def recover(config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Resolve orders left pending by an interrupted run."""
    _print(_system(config, venue=venue).adapter.recover())


@app.command()
def mtm(config: ConfigOpt = None, venue: VenueOpt = None,
        close: Annotated[bool, typer.Option(help="Record as the session close mark (credits dividends)")] = False) -> None:
    """Mark the portfolio to market now."""
    _print(_system(config, venue=venue).mark_to_market("close" if close else "intraday"))


@app.command()
def kill(reason: Annotated[str, typer.Option(help="Why trading is being stopped")],
         actor: Annotated[str, typer.Option()] = "operator", config: ConfigOpt = None, venue: VenueOpt = None,
         venue_stop: Annotated[bool, typer.Option(help="Also call QuantDinger's kill switch (revokes trade tokens)")] = False) -> None:
    """Activate the kill switch: no new orders until `aipt resume`."""
    _print(_system(config, venue=venue).kill(actor, reason, venue_stop=venue_stop))


@app.command()
def resume(reviewer: Annotated[str, typer.Option()], note: Annotated[str, typer.Option()],
           config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Deactivate the kill switch after review."""
    _system(config, venue=venue).resume(reviewer, note)
    typer.echo("kill switch cleared")


@app.command("halt-clear")
def halt_clear(reviewer: Annotated[str, typer.Option()], note: Annotated[str, typer.Option()],
               config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Clear the drawdown halt after manual review; the drawdown peak restarts from now."""
    _system(config, venue=venue).clear_drawdown_halt(reviewer, note)
    typer.echo("drawdown halt cleared")


@app.command("reconcile")
def reconcile_cmd(config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Check ledger cash/positions/equity identity and ledger-vs-venue orders."""
    result = _system(config, venue=venue).reconcile()
    _print(result)
    raise typer.Exit(0 if result["ok"] else 1)


@app.command()
def report(config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Regenerate dashboard and SavvyTrader publication files."""
    system = _system(config, venue=venue)
    _print(system.publish())


@app.command()
def scheduler(config: ConfigOpt = None, research: ResearchOpt = "tradingagents", venue: VenueOpt = None) -> None:
    """Run the daily scheduler in the foreground (decision slots + close mark)."""
    from aipt.workflow.scheduler import Scheduler

    system = _system(config, research=research, venue=venue, with_research=True)
    system.init_account()
    Scheduler(system).run_forever()


@app.command("qd-check")
def qd_check(config: ConfigOpt = None) -> None:
    """Verify the QuantDinger token is paper-only and the gateway is reachable."""
    from aipt.config import Secrets
    from aipt.execution.venues import QuantDingerClient, QuantDingerPaperVenue

    settings = _settings(config)
    s = Secrets.from_env()
    if not (s.quantdinger_base_url and s.quantdinger_agent_token):
        raise typer.BadParameter("QUANTDINGER_BASE_URL / QUANTDINGER_AGENT_TOKEN not set")
    client = QuantDingerClient(s.quantdinger_base_url, s.quantdinger_agent_token)
    venue = QuantDingerPaperVenue(client, settings.execution.quantdinger_market)
    _print({"health": client.health(), "whoami": venue.verify_paper_only()})


@app.command()
def backtest(start: Annotated[str, typer.Option(help="YYYY-MM-DD")], end: Annotated[str, typer.Option(help="YYYY-MM-DD")],
             name: Annotated[str, typer.Option()] = "default", research: ResearchOpt = "scripted",
             tickers: Annotated[str | None, typer.Option()] = None, config: ConfigOpt = None) -> None:
    """Chronological daily backtest into a separate HISTORICAL_BACKTEST ledger."""
    from aipt.backtest import run_backtest

    settings = _settings(config)
    if research == "tradingagents":
        from aipt.system import check_llm_credentials

        check_llm_credentials(settings)
    out = run_backtest(settings, date.fromisoformat(start), date.fromisoformat(end), name=name, research=research,
                       tickers=tickers.split(",") if tickers else None)
    perf = out["performance"]
    _print({k: out[k] for k in ("label", "start", "end", "research", "flags", "final_cash", "final_positions")} |
           {"equity": perf["equity"], "cumulative_return": perf["cumulative_return"],
            "max_drawdown": perf["max_drawdown"], "sharpe": perf["sharpe"], "benchmark": {
                k: v for k, v in (perf.get("benchmark") or {}).items() if k not in ("curve",)},
            "failed_sessions": [s for s in out["sessions"] if s["status"] != "completed"]})


@app.command("replay")
def replay(day: Annotated[str, typer.Option("--date", help="A past NYSE session, YYYY-MM-DD")],
           at: Annotated[str, typer.Option(help="Decision time, New York, HH:MM")] = "10:30",
           research: ResearchOpt = "scripted", tickers: Annotated[str | None, typer.Option()] = None,
           config: ConfigOpt = None) -> None:
    """Replay one full paper cycle on a past session with minute bars (separate REPLAY_DEMO ledger)."""
    from zoneinfo import ZoneInfo

    from aipt.clock import SimulatedClock
    from aipt.market.data import BarStore
    from aipt.system import build_replay

    settings = _settings(config)
    d = date.fromisoformat(day)
    root, reports = Path(settings.paths.data_dir) / "replays" / day, Path(settings.paths.reports_dir) / "replays" / day
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(reports, ignore_errors=True)
    rs = settings.model_copy(update={
        "paths": settings.paths.model_copy(update={"data_dir": root, "reports_dir": reports}),
        "account": settings.account.model_copy(update={"id": f"{settings.account.id}-replay-{day}"})}, deep=True)
    universe = tickers.split(",") if tickers else rs.research.watchlist[: rs.research.max_tickers_per_cycle]
    store = BarStore.from_yahoo(list(dict.fromkeys(universe + [rs.benchmark.ticker])), d - timedelta(days=120), d,
                                intraday=True, intraday_start=d)
    h, m = (int(x) for x in at.split(":"))
    clock = SimulatedClock(datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo("America/New_York")))
    system = build_replay(rs, store=store, clock=clock, research=research)
    system.init_account(label="REPLAY_DEMO")
    initial = _portfolio(system)
    result = system.run_cycle(f"slot-{at.replace(':', '')}", tickers=universe)
    clock.set(system.calendar.session_close(d) + timedelta(minutes=10))
    system.mark_to_market("close")
    system.publish()
    _print({"label": "REPLAY_DEMO", "session": day, "decision_time_et": at, "research": research,
            "run_id": result.run_id, "status": result.status, "initial_portfolio": initial,
            "decisions": result.detail.get("decisions"), "execution": result.detail.get("execution"),
            "reconciliation_ok": (result.detail.get("reconciliation") or {}).get("ok"),
            "final_portfolio_at_close": _portfolio(system), "reports_dir": str(reports)})


@app.command()
def dashboard(config: ConfigOpt = None, venue: VenueOpt = None,
              serve: Annotated[bool, typer.Option(help="Serve reports/ on http://127.0.0.1:8765")] = False) -> None:
    """Regenerate (and optionally serve) the HTML dashboard."""
    system = _system(config, venue=venue)
    out = system.publish()
    typer.echo(out["dashboard"])
    if serve:
        import functools
        import http.server

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(system.reports_dir))
        with http.server.ThreadingHTTPServer(("127.0.0.1", 8765), handler) as httpd:
            typer.echo("serving http://127.0.0.1:8765/dashboard.html (Ctrl-C to stop)")
            httpd.serve_forever()


@savvy.command("queue")
def savvy_queue(config: ConfigOpt = None, venue: VenueOpt = None,
                all_entries: Annotated[bool, typer.Option("--all")] = False) -> None:
    """List trades waiting to be entered manually on SavvyTrader."""
    system = _system(config, venue=venue)
    system.publisher.sync_queue()
    _print(system.ledger.publication_entries(None if all_entries else "pending"))


@savvy.command("confirm")
def savvy_confirm(entry_id: int, price: Annotated[float, typer.Option(help="Price SavvyTrader executed at")],
                  entered_by: Annotated[str, typer.Option()],
                  quantity: Annotated[float | None, typer.Option(help="Shares entered on SavvyTrader")] = None,
                  executed_at: Annotated[str | None, typer.Option(help="SavvyTrader execution time (ISO)")] = None,
                  note: str = "", config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Record that an entry was placed on SavvyTrader, with SavvyTrader's own execution price."""
    system = _system(config, venue=venue)
    _print(system.publisher.confirm(entry_id, price, quantity, executed_at, entered_by, note))


@savvy.command("skip")
def savvy_skip(entry_id: int, entered_by: Annotated[str, typer.Option()], note: Annotated[str, typer.Option()],
               config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Mark an entry as deliberately not published."""
    _print(_system(config, venue=venue).publisher.skip(entry_id, entered_by, note))


@savvy.command("discrepancies")
def savvy_discrepancies(config: ConfigOpt = None, venue: VenueOpt = None) -> None:
    """Differences between local simulated fills and SavvyTrader's recorded executions."""
    _print(_system(config, venue=venue).publisher.discrepancies())


if __name__ == "__main__":
    app()
