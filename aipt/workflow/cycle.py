"""The decision cycle and account operations.

A cycle's run_id is ``<account>:<session date>:<slot>``. Re-running the same slot (a retried
job, a restarted process) resumes that run: completed steps are skipped, tickers already
decided are not re-researched, and orders map onto the same idempotency keys. A process-wide
file lock stops two cycles from running at once.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from aipt.clock import Clock
from aipt.config import Settings
from aipt.costs import UsageTracker
from aipt.execution.adapter import ExecutionAdapter
from aipt.execution.venues import PaperVenue
from aipt.ledger import DRAWDOWN_HALT, KILL_SWITCH, Ledger
from aipt.market.calendar import USEquityCalendar
from aipt.market.data import MarketData
from aipt.models import Action, DecisionStatus, OrderStatus, TradeDecision
from aipt.portfolio import performance
from aipt.portfolio.reconcile import VENUE_LIST_CAP, reconcile
from aipt.publishing.savvytrader import SavvyTraderPublisher
from aipt.research import decision as decision_validation

logger = logging.getLogger(__name__)


class CycleLocked(RuntimeError):
    pass


class KillSwitchActive(RuntimeError):
    pass


@dataclass
class CycleResult:
    run_id: str
    status: str
    detail: dict[str, Any]


class TradingSystem:
    def __init__(self, settings: Settings, *, clock: Clock, ledger: Ledger, market_data: MarketData,
                 venue: PaperVenue, researcher=None, formalizer=None, tracker: UsageTracker | None = None,
                 calendar: USEquityCalendar | None = None):
        if settings.mode != "paper":
            raise RuntimeError("only paper mode is supported")
        self.settings = settings
        self.clock = clock
        self.ledger = ledger
        self.md = market_data
        self.venue = venue
        self.researcher = researcher
        self.formalizer = formalizer
        self.tracker = tracker
        self.calendar = calendar or USEquityCalendar()
        self.adapter = ExecutionAdapter(settings, ledger, venue, market_data, clock, self.calendar)
        self.publisher = SavvyTraderPublisher(settings, ledger)
        self.reports_dir = Path(settings.paths.reports_dir)

    # ------------------------------------------------------------ account

    def init_account(self, label: str = "LIVE_FORWARD_PAPER") -> bool:
        a = self.settings.account
        created = self.ledger.create_account(a.name, a.initial_capital, a.base_currency, label=label,
                                             now=self.clock.now())
        if created:
            now = self.clock.now()
            self.ledger.record_snapshot(ts=now, session_date=self.calendar.session_for(now).session_date,
                                        cash=a.initial_capital, market_value=0.0, prices={}, kind="inception")
        return created

    # ------------------------------------------------------------ controls

    def kill(self, actor: str, reason: str, venue_stop: bool = False) -> dict[str, Any]:
        self.ledger.set_control(KILL_SWITCH, {"active": True}, actor=actor, note=reason, now=self.clock.now())
        out: dict[str, Any] = {"kill_switch": True}
        if venue_stop:
            out["venue"] = self.venue.emergency_stop(f"aipt-kill-{self.clock.now():%Y%m%dT%H%M%S}")
        return out

    def resume(self, actor: str, note: str) -> None:
        self.ledger.set_control(KILL_SWITCH, {"active": False}, actor=actor, note=note, now=self.clock.now())

    def clear_drawdown_halt(self, reviewer: str, note: str) -> None:
        if not note.strip():
            raise ValueError("a review note is required to clear the drawdown halt")
        self.ledger.set_control(DRAWDOWN_HALT, {"active": False, "reset_peak": True}, actor=reviewer, note=note,
                                now=self.clock.now())

    # ------------------------------------------------------- mark to market

    def mark_to_market(self, kind: str = "intraday") -> dict[str, Any]:
        now = self.clock.now()
        session_date = self.calendar.session_for(now).session_date
        if kind == "close":
            self._credit_dividends(session_date)
        snap, _ = self.adapter.snapshot()
        result = self.ledger.record_snapshot(ts=now, session_date=session_date, cash=snap.cash,
                                             market_value=snap.market_value,
                                             prices={t: q.price for t, q in snap.prices.items()}, kind=kind)
        halt = self.ledger.control(DRAWDOWN_HALT)
        dd = self._drawdown_since_review(result)
        if dd >= self.settings.risk.drawdown_halt and not halt.get("active"):
            self.ledger.set_control(DRAWDOWN_HALT, {"active": True, "drawdown": dd, "equity": result["equity"]},
                                    actor="system", note=f"drawdown {dd:.2%} reached the "
                                                         f"{self.settings.risk.drawdown_halt:.0%} halt level", now=now)
            logger.warning("drawdown halt engaged at %.2f%%", dd * 100)
        return {**result, "drawdown_since_review": dd, "cash": snap.cash, "market_value": snap.market_value,
                "prices": {t: q.model_dump(mode="json") for t, q in snap.prices.items()}}

    def _drawdown_since_review(self, latest: dict[str, float]) -> float:
        """Drawdown from the running peak, where a reviewed halt clearance restarts the peak."""
        halt = self.ledger.control(DRAWDOWN_HALT)
        snaps = self.ledger.snapshots()
        if halt.get("reset_peak") and halt.get("updated_at"):
            snaps = [s for s in snaps if s["ts"] >= halt["updated_at"]] or snaps[-1:]
        peak = max([s["equity"] for s in snaps] + [latest["equity"]])
        return 0.0 if peak <= 0 else (peak - latest["equity"]) / peak

    def _credit_dividends(self, session_date: date) -> None:
        fills = self.ledger.fills()
        for ticker in self.ledger.positions():
            try:
                divs = self.md.dividends(ticker, session_date - timedelta(days=10), session_date)
            except Exception as exc:
                logger.warning("dividends unavailable for %s: %s", ticker, exc)
                continue
            for ex_date, amount in divs.items():
                ex_open = self.calendar.session_open(ex_date.date()) if self.calendar.is_trading_day(ex_date.date()) \
                    else None
                if ex_open is None:
                    continue
                held = sum((f["quantity"] if f["side"] == "buy" else -f["quantity"]) for f in fills
                           if f["ticker"] == ticker and f["ts"] < ex_open.isoformat())
                if self.ledger.record_dividend(ticker, ex_date.date(), float(amount), held, self.clock.now()):
                    logger.info("credited %s dividend %.4f x %s", ticker, amount, held)

    # --------------------------------------------------------------- cycle

    @contextmanager
    def _lock(self):
        path = Path(self.settings.paths.data_dir) / "cycle.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise CycleLocked("another cycle is running") from exc
        try:
            fh.write(str(os.getpid()))
            fh.flush()
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    def select_tickers(self, explicit: list[str] | None = None) -> list[str]:
        if explicit:
            return [t.upper() for t in dict.fromkeys(explicit)]
        held = list(self.ledger.positions())
        watch = [t for t in self.settings.research.watchlist if t not in held]
        room = max(self.settings.research.max_tickers_per_cycle - len(held), 0)
        return held + watch[:room]

    def _spent_today(self, session_date: date) -> float:
        return float(self.ledger.llm_usage_summary(since=session_date.isoformat())["priced_cost_usd"])

    def research(self, run_id: str, tickers: list[str], session_date: date,
                 analysis_date: date | None = None) -> list[TradeDecision]:
        decisions = []
        for ticker in tickers:
            existing = self.ledger.decision_for(run_id, ticker)
            if existing:
                decisions.append(existing)
                continue
            if self.ledger.control(KILL_SWITCH).get("active"):
                raise KillSwitchActive("kill switch activated during research")
            budget = self.settings.research.daily_llm_budget_usd
            if budget and self._spent_today(session_date) >= budget:
                logger.warning("daily LLM budget %.2f reached; stopping research", budget)
                break
            decisions.append(self._research_one(run_id, ticker, analysis_date or session_date))
        return decisions

    def _research_one(self, run_id: str, ticker: str, analysis_date: date) -> TradeDecision:
        snap, _ = self.adapter.snapshot([ticker])
        current_weight = snap.weight(ticker)
        trade_date = analysis_date.isoformat()
        if self.tracker:
            self.tracker.set_context(run_id=run_id, ticker=ticker)
        timestamps = {"analysis_date": trade_date, "snapshot_at": snap.timestamp.isoformat()}
        if ticker in snap.prices:
            timestamps["decision_quote_at"] = snap.prices[ticker].timestamp.isoformat()
            timestamps["decision_quote_source"] = snap.prices[ticker].source
        final_state, signal, proposal, error = {}, None, None, None
        model_info = {"quick_model": self.settings.llm.quick_model, "deep_model": self.settings.llm.deep_model,
                      "provider": self.settings.llm.provider,
                      "formalizer_model": self.formalizer.model_name() if self.formalizer else None}
        try:
            result = self.researcher.analyze(ticker, trade_date, snap.cash, snap.positions)
            final_state, signal = result["final_state"], result["signal"]
            timestamps["research_started_at"], timestamps["research_finished_at"] = \
                result["started_at"], result["finished_at"]
            proposal, error = self.formalizer.formalize(ticker, trade_date, final_state, current_weight)
        except Exception as exc:
            logger.exception("research failed for %s", ticker)
            error = f"research error: {type(exc).__name__}: {exc}"
        decision = decision_validation.validate(
            run_id=run_id, account_id=self.ledger.account_id, ticker=ticker, trade_date=trade_date,
            final_state=final_state, signal=signal, proposal=proposal, formalizer_error=error,
            current_weight=current_weight, allocation=self.settings.allocation, now=self.clock.now(),
            data_timestamps=timestamps, model_info=model_info)
        self.ledger.record_decision(decision)
        if self.tracker:
            self.ledger.record_llm_usage(self.tracker.drain(), self.clock.now())
        logger.info("decision %s %s %s -> %s target=%s", decision.decision_id, ticker, decision.rating.value,
                    decision.status.value, decision.target_weight)
        return decision

    def run_cycle(self, slot: str, tickers: list[str] | None = None, execute: bool = True,
                  analysis_date: date | None = None) -> CycleResult:
        """One decision cycle. ``analysis_date`` (default: the session date) is the date research
        may see data up to; backtests pass the previous session so decisions use closed bars only."""
        now = self.clock.now()
        session = self.calendar.session_for(now)
        run_id = f"{self.ledger.account_id}:{session.session_date}:{slot}"
        with self._lock():
            run = self.ledger.start_run(run_id, "decision_cycle", session.session_date, now)
            if run["status"] == "completed":
                return CycleResult(run_id, "already_completed", {"note": "this slot already completed; not re-run"})
            done = (self.ledger.run(run_id) or {}).get("steps", {})
            detail: dict[str, Any] = {}
            try:
                if self.ledger.control(KILL_SWITCH).get("active"):
                    raise KillSwitchActive("kill switch is active; cycle not started")
                self.ledger.account()
                detail["recovered"] = self.adapter.recover()
                detail["pre_trade_mark"] = self.mark_to_market("intraday")
                if "research" in done:
                    decisions = self.ledger.decisions(run_id=run_id)
                else:
                    chosen = self.select_tickers(tickers)
                    decisions = self.research(run_id, chosen, session.session_date, analysis_date)
                    self.ledger.mark_step(run_id, "research", {"tickers": chosen, "decisions": len(decisions)})
                detail["decisions"] = [_decision_summary(d) for d in decisions]
                pending: list[str] = []
                if execute and "execute" not in done:
                    report = self.adapter.execute(decisions, run_id)
                    detail["execution"] = report.as_dict()
                    pending = [o["order_id"] for o in self.ledger.orders(status=[OrderStatus.PENDING_SUBMIT])
                               if o["run_id"] == run_id]
                    if not pending:
                        self.ledger.mark_step(run_id, "execute", {k: len(v) for k, v in report.as_dict().items()})
                detail["post_trade_mark"] = self.mark_to_market("intraday")
                detail["reconciliation"] = self.reconcile()
                self.ledger.mark_step(run_id, "reconcile", {"ok": detail["reconciliation"]["ok"]})
                detail["publication"] = self.publish()
                if pending:
                    # Retrying this slot resumes it: recover() resubmits these with their original keys.
                    status = "incomplete"
                    detail["error"] = f"orders not acknowledged by the venue: {pending}"
                    self.ledger.finish_run(run_id, status, detail["error"], now=self.clock.now())
                else:
                    status = "completed"
                    self.ledger.finish_run(run_id, status, now=self.clock.now())
            except KillSwitchActive as exc:
                self.ledger.finish_run(run_id, "halted", str(exc), now=self.clock.now())
                status, detail["error"] = "halted", str(exc)
            except Exception as exc:
                logger.exception("cycle %s failed", run_id)
                self.ledger.finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}", now=self.clock.now())
                status, detail["error"] = "failed", f"{type(exc).__name__}: {exc}"
            self._write_run_report(run_id, status, detail)
            return CycleResult(run_id, status, detail)

    # ------------------------------------------------------- reconcile/publish

    def reconcile(self) -> dict[str, Any]:
        snap, _ = self.adapter.snapshot()
        try:
            venue_orders = self.venue.list_orders()
        except Exception as exc:
            logger.warning("venue order list unavailable: %s", exc)
            venue_orders = None
        cap = VENUE_LIST_CAP if self.venue.name == "quantdinger" else None
        result = reconcile(self.ledger, venue_orders, snap.prices, venue_list_cap=cap)
        if not result["ok"]:
            logger.error("reconciliation issues: %s", result["issues"])
        self.ledger.audit("reconciliation", {"ok": result["ok"], "issues": result["issues"]})
        return result

    def performance(self) -> dict[str, Any]:
        return performance.compute(self.ledger, self.md, self.settings.benchmark.ticker,
                                   self.settings.benchmark.risk_free_rate, self.calendar)

    def publish(self) -> dict[str, Any]:
        from aipt.dashboard import write_dashboard

        added = self.publisher.sync_queue(self.clock.now())
        perf = self.performance()
        snap, _ = self.adapter.snapshot()
        paths = self.publisher.write(perf, snap.prices)
        dash = write_dashboard(self, perf, snap)
        return {"queued_new_entries": added, "files": {k: str(v) for k, v in paths.items()}, "dashboard": str(dash)}

    def _write_run_report(self, run_id: str, status: str, detail: dict[str, Any]) -> Path:
        out = self.reports_dir / "runs"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{run_id.replace(':', '_')}.json"
        path.write_text(json.dumps({"run_id": run_id, "status": status, "detail": detail}, indent=2, default=str),
                        encoding="utf-8")
        return path


def _decision_summary(d: TradeDecision) -> dict[str, Any]:
    return {"decision_id": d.decision_id, "ticker": d.ticker, "rating": d.rating.value, "action": d.action.value,
            "status": d.status.value, "target_weight": d.target_weight, "current_weight": d.current_weight,
            "confidence": d.confidence, "rejection_reasons": d.rejection_reasons, "notes": d.notes,
            "tradeable": d.status is DecisionStatus.ACCEPTED and d.action is not Action.HOLD}
