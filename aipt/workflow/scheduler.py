"""Daily scheduler: decision cycles at configured New York times on NYSE sessions, plus a
post-close mark-to-market. Not tick-driven.

Each slot's run_id is deterministic, so a restart re-enters an unfinished slot (resuming it)
and skips a finished one. A slot whose time has passed by more than ``catch_up_minutes``
when the process starts is not run late (its orders would miss the intended window).
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aipt.ledger import KILL_SWITCH
from aipt.workflow.cycle import CycleLocked, TradingSystem

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, system: TradingSystem, catch_up_minutes: int = 90):
        self.system = system
        self.tz = ZoneInfo(system.settings.schedule.timezone)
        self.catch_up = timedelta(minutes=catch_up_minutes)
        self._stop = False
        self.heartbeat = Path(system.settings.paths.data_dir) / "scheduler.heartbeat"

    def stop(self, *_args) -> None:
        logger.info("scheduler stopping after the current step")
        self._stop = True

    def _at(self, day, hhmm: str) -> datetime:
        h, m = (int(x) for x in hhmm.split(":"))
        return datetime(day.year, day.month, day.day, h, m, tzinfo=self.tz)

    def due(self, now: datetime) -> list[tuple[str, datetime]]:
        local = now.astimezone(self.tz)
        if not self.system.calendar.is_trading_day(local.date()):
            return []
        sched = self.system.settings.schedule
        slots = [(f"slot-{t.replace(':', '')}", self._at(local.date(), t)) for t in sched.decision_times]
        slots.append(("mtm-close", self._at(local.date(), sched.mark_to_market_time)))
        return [(name, at) for name, at in slots if at <= local < at + self.catch_up]

    def tick(self) -> list[str]:
        now = self.system.clock.now()
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.write_text(now.isoformat())
        ran = []
        session = self.system.calendar.session_for(now).session_date
        for name, _at in self.due(now):
            run_id = f"{self.system.ledger.account_id}:{session}:{name}"
            existing = self.system.ledger.run(run_id)
            if existing and existing["status"] in ("completed", "halted"):
                continue
            if name == "mtm-close":
                self.system.ledger.start_run(run_id, "mark_to_market", session, now)
                try:
                    self.system.mark_to_market("close")
                    self.system.reconcile()
                    self.system.publish()
                    self.system.ledger.finish_run(run_id, "completed", now=self.system.clock.now())
                except Exception as exc:
                    logger.exception("close mark failed")
                    self.system.ledger.finish_run(run_id, "failed", str(exc), now=self.system.clock.now())
            else:
                if self.system.ledger.control(KILL_SWITCH).get("active"):
                    logger.warning("kill switch active; skipping %s", name)
                    continue
                try:
                    result = self.system.run_cycle(name)
                    logger.info("cycle %s -> %s", result.run_id, result.status)
                except CycleLocked:
                    logger.warning("cycle already running; skipping this tick")
            ran.append(name)
        return ran

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        logger.info("scheduler started (decision times %s ET, close mark %s ET)",
                    self.system.settings.schedule.decision_times, self.system.settings.schedule.mark_to_market_time)
        while not self._stop:
            try:
                self.tick()
            except Exception:
                logger.exception("scheduler tick failed; continuing")
            for _ in range(self.system.settings.schedule.poll_seconds):
                if self._stop:
                    break
                time.sleep(1)
