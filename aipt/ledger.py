"""The authoritative paper ledger (SQLite, WAL).

Account state is owned here. QuantDinger's paper endpoint records orders and supplies the
fill reference price, but it keeps no cash balance or positions, so cash and holdings are
derived only from this ledger's rows:

- cash      = SUM(cash_entries.amount)          (deposit, trade, dividend)
- positions = maintained transactionally with each fill (quantity, average cost incl. costs)

Applying a fill is one transaction keyed by order_id, so a retried job cannot apply a fill
twice. Average cost includes buy-side costs, and realized P&L is net of sell-side costs, so
equity == initial capital + dividends + realized + unrealized holds exactly; reconciliation
checks that identity.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from aipt.models import (
    Fill,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
    RiskViolation,
    TradeDecision,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_currency TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    mode TEXT NOT NULL CHECK (mode = 'paper'),
    label TEXT NOT NULL,                 -- LIVE_FORWARD_PAPER | HISTORICAL_BACKTEST | REPLAY_DEMO | TEST
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(account_id),
    ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('deposit','trade','dividend')),
    amount REAL NOT NULL,
    ref TEXT,
    UNIQUE (account_id, kind, ref)
);
CREATE TABLE IF NOT EXISTS positions (
    account_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    quantity REAL NOT NULL,
    average_cost REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, ticker)
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    status TEXT NOT NULL,
    rating TEXT NOT NULL,
    action TEXT NOT NULL,
    target_weight REAL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, ticker)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    run_id TEXT,
    decision_id TEXT,
    session_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy','sell')),
    requested_quantity REAL NOT NULL,
    quantity REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    violations TEXT,
    reference_quote TEXT,
    estimated_notional REAL,
    venue TEXT,
    venue_order_id TEXT,
    venue_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
    account_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    reference_price REAL NOT NULL,
    fill_price REAL NOT NULL,
    slippage_bps REAL NOT NULL,
    commission REAL NOT NULL,
    fees REAL NOT NULL,
    notional REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    ts TEXT NOT NULL,
    venue TEXT NOT NULL,
    venue_order_id TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    session_date TEXT NOT NULL,
    cash REAL NOT NULL,
    market_value REAL NOT NULL,
    equity REAL NOT NULL,
    peak_equity REAL NOT NULL,
    drawdown REAL NOT NULL,
    kind TEXT NOT NULL,              -- intraday | close
    prices TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS controls (
    account_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    actor TEXT,
    note TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, key)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    session_date TEXT NOT NULL,
    status TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    run_id TEXT,
    ticker TEXT,
    role TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publication_queue (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    fill_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    local_fill_price REAL NOT NULL,
    local_fill_ts TEXT NOT NULL,
    comment TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | entered | skipped
    savvy_price REAL,
    savvy_quantity REAL,
    savvy_ts TEXT,
    entered_by TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

KILL_SWITCH = "kill_switch"
DRAWDOWN_HALT = "drawdown_halt"


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


class LedgerError(RuntimeError):
    pass


class Ledger:
    def __init__(self, path: Path | str, account_id: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account_id = account_id
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ account

    def create_account(self, name: str, initial_capital: float, base_currency: str = "USD",
                       label: str = "LIVE_FORWARD_PAPER", now: datetime | None = None) -> bool:
        """Create the account funded with ``initial_capital`` in cash. Returns False if it exists."""
        now = now or utcnow()
        with self.tx() as c:
            existing = c.execute("SELECT * FROM accounts WHERE account_id=?", (self.account_id,)).fetchone()
            if existing:
                if abs(existing["initial_capital"] - initial_capital) > 1e-9 or existing["base_currency"] != base_currency:
                    raise LedgerError(
                        f"account {self.account_id} exists with capital {existing['initial_capital']} "
                        f"{existing['base_currency']}; refusing to change it")
                return False
            c.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)",
                      (self.account_id, name, base_currency, initial_capital, "paper", label, _iso(now)))
            c.execute("INSERT INTO cash_entries (account_id, ts, kind, amount, ref) VALUES (?,?,?,?,?)",
                      (self.account_id, _iso(now), "deposit", initial_capital, "initial-capital"))
            self._audit(c, "account_created", {"name": name, "initial_capital": initial_capital,
                                               "currency": base_currency, "label": label}, now)
        return True

    def account(self) -> dict[str, Any]:
        row = self._one("SELECT * FROM accounts WHERE account_id=?", (self.account_id,))
        if row is None:
            raise LedgerError(f"account {self.account_id} not initialised; run `aipt init`")
        return dict(row)

    def cash(self) -> float:
        row = self._one("SELECT COALESCE(SUM(amount),0) AS cash FROM cash_entries WHERE account_id=?",
                        (self.account_id,))
        return float(row["cash"])

    def positions(self, include_flat: bool = False) -> dict[str, Position]:
        rows = self._all("SELECT * FROM positions WHERE account_id=? ORDER BY ticker", (self.account_id,))
        return {r["ticker"]: Position(ticker=r["ticker"], quantity=r["quantity"], average_cost=r["average_cost"],
                                      realized_pnl=r["realized_pnl"])
                for r in rows if include_flat or abs(r["quantity"]) > 1e-12}

    def total_dividends(self) -> float:
        row = self._one("SELECT COALESCE(SUM(amount),0) v FROM cash_entries WHERE account_id=? AND kind='dividend'",
                        (self.account_id,))
        return float(row["v"])

    def total_realized(self) -> float:
        row = self._one("SELECT COALESCE(SUM(realized_pnl),0) v FROM positions WHERE account_id=?", (self.account_id,))
        return float(row["v"])

    # ---------------------------------------------------------------- decisions

    def record_decision(self, decision: TradeDecision) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (decision.decision_id, self.account_id, decision.run_id, decision.ticker, decision.status.value,
                 decision.rating.value, decision.action.value, decision.target_weight,
                 decision.model_dump_json(), _iso(decision.decision_timestamp)))

    def decision_for(self, run_id: str, ticker: str) -> TradeDecision | None:
        row = self._one("SELECT payload FROM decisions WHERE run_id=? AND ticker=?", (run_id, ticker))
        return TradeDecision.model_validate_json(row["payload"]) if row else None

    def decisions(self, run_id: str | None = None, limit: int = 200) -> list[TradeDecision]:
        if run_id:
            rows = self._all("SELECT payload FROM decisions WHERE account_id=? AND run_id=? ORDER BY created_at",
                             (self.account_id, run_id))
        else:
            rows = self._all("SELECT payload FROM decisions WHERE account_id=? ORDER BY created_at DESC LIMIT ?",
                             (self.account_id, limit))
        return [TradeDecision.model_validate_json(r["payload"]) for r in rows]

    # ------------------------------------------------------------------- orders

    def create_order(self, *, idempotency_key: str, intent: OrderIntent, session_date: date, run_id: str | None,
                     venue: str, now: datetime) -> tuple[dict[str, Any], bool]:
        """Persist an order intent before it reaches the venue. Returns (order, created)."""
        with self.tx() as c:
            existing = c.execute("SELECT * FROM orders WHERE account_id=? AND idempotency_key=?",
                                 (self.account_id, idempotency_key)).fetchone()
            if existing:
                return dict(existing), False
            order_id = f"ord_{uuid.uuid4().hex[:16]}"
            c.execute(
                """INSERT INTO orders (order_id, account_id, idempotency_key, run_id, decision_id, session_date,
                   ticker, side, requested_quantity, quantity, status, reason, reference_quote, estimated_notional,
                   venue, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, self.account_id, idempotency_key, run_id, intent.decision_id, session_date.isoformat(),
                 intent.ticker, intent.side.value, intent.requested_quantity, intent.quantity,
                 OrderStatus.PENDING_SUBMIT.value, intent.reason, intent.reference_quote.model_dump_json(),
                 intent.estimated_notional, venue, _iso(now), _iso(now)))
            row = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            return dict(row), True

    def record_rejection(self, *, idempotency_key: str, intent: OrderIntent, session_date: date, run_id: str | None,
                         violations: list[RiskViolation], now: datetime) -> dict[str, Any]:
        """Record an order blocked before the venue, with its reasons. Idempotent on the key."""
        order, created = self.create_order(idempotency_key=idempotency_key, intent=intent, session_date=session_date,
                                           run_id=run_id, venue="none", now=now)
        if created or order["status"] == OrderStatus.PENDING_SUBMIT.value:
            self.update_order(order["order_id"], status=OrderStatus.REJECTED,
                              violations=[v.model_dump() for v in violations], now=now)
        return dict(self._one("SELECT * FROM orders WHERE order_id=?", (order["order_id"],)))

    def update_order(self, order_id: str, *, status: OrderStatus, now: datetime, venue_order_id: str | None = None,
                     venue_status: str | None = None, violations: list[dict] | None = None,
                     reason: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE orders SET status=?, updated_at=?, venue_order_id=COALESCE(?, venue_order_id),
                   venue_status=COALESCE(?, venue_status), violations=COALESCE(?, violations),
                   reason=COALESCE(?, reason) WHERE order_id=?""",
                (status.value, _iso(now), venue_order_id, venue_status,
                 json.dumps(violations) if violations is not None else None, reason, order_id))

    def order(self, order_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM orders WHERE order_id=?", (order_id,))
        return dict(row) if row else None

    def orders(self, *, status: list[OrderStatus] | None = None, session_date: date | None = None,
               limit: int = 1000) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM orders WHERE account_id=?", [self.account_id]
        if status:
            sql += f" AND status IN ({','.join('?' * len(status))})"
            args += [s.value for s in status]
        if session_date:
            sql += " AND session_date=?"
            args.append(session_date.isoformat())
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._all(sql, tuple(args))]

    def session_order_count(self, session_date: date, ticker: str, side: OrderSide) -> int:
        """Orders that reached (or are reaching) the venue for this ticker and side this session."""
        live = (OrderStatus.PENDING_SUBMIT.value, OrderStatus.FILLED.value, OrderStatus.PARTIALLY_FILLED.value)
        row = self._one(
            f"""SELECT COUNT(*) n FROM orders WHERE account_id=? AND session_date=? AND ticker=? AND side=?
                AND status IN ({','.join('?' * len(live))})""",
            (self.account_id, session_date.isoformat(), ticker, side.value, *live))
        return int(row["n"])

    def session_buy_notional(self, session_date: date) -> float:
        row = self._one(
            """SELECT COALESCE(SUM(f.notional),0) v FROM fills f JOIN orders o ON o.order_id=f.order_id
               WHERE f.account_id=? AND o.session_date=? AND f.side='buy'""",
            (self.account_id, session_date.isoformat()))
        pending = self._one(
            "SELECT COALESCE(SUM(estimated_notional),0) v FROM orders WHERE account_id=? AND session_date=? "
            "AND side='buy' AND status=?", (self.account_id, session_date.isoformat(), OrderStatus.PENDING_SUBMIT.value))
        return float(row["v"]) + float(pending["v"])

    # -------------------------------------------------------------------- fills

    def apply_fill(self, fill: Fill, final_status: OrderStatus, venue_status: str | None = None) -> bool:
        """Apply a fill atomically: fill row, position, cash, order status. False if already applied."""
        with self.tx() as c:
            if c.execute("SELECT 1 FROM fills WHERE order_id=?", (fill.order_id,)).fetchone():
                return False
            pos = c.execute("SELECT * FROM positions WHERE account_id=? AND ticker=?",
                            (self.account_id, fill.ticker)).fetchone()
            qty = pos["quantity"] if pos else 0.0
            avg = pos["average_cost"] if pos else 0.0
            realized_total = pos["realized_pnl"] if pos else 0.0
            costs = fill.commission + fill.fees
            realized = 0.0
            if fill.side is OrderSide.BUY:
                cash_delta = -(fill.quantity * fill.fill_price + costs)
                new_qty = qty + fill.quantity
                new_avg = (qty * avg + fill.quantity * fill.fill_price + costs) / new_qty
            else:
                if fill.quantity > qty + 1e-9:
                    raise LedgerError(f"sell of {fill.quantity} {fill.ticker} exceeds holding {qty}; no short selling")
                cash_delta = fill.quantity * fill.fill_price - costs
                realized = fill.quantity * (fill.fill_price - avg) - costs
                new_qty = qty - fill.quantity
                new_avg = avg if new_qty > 1e-12 else 0.0
                new_qty = new_qty if new_qty > 1e-12 else 0.0
            cash_now = c.execute("SELECT COALESCE(SUM(amount),0) v FROM cash_entries WHERE account_id=?",
                                 (self.account_id,)).fetchone()["v"]
            if cash_now + cash_delta < -1e-6:
                raise LedgerError(f"fill would take cash to {cash_now + cash_delta:.2f}; no margin or borrowing")
            fill_id = f"fil_{uuid.uuid4().hex[:16]}"
            c.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (fill_id, fill.order_id, self.account_id, fill.ticker, fill.side.value, fill.quantity,
                       fill.reference_price, fill.fill_price, fill.slippage_bps, fill.commission, fill.fees,
                       fill.notional, realized, _iso(fill.timestamp), fill.venue, fill.venue_order_id))
            c.execute(
                """INSERT INTO positions VALUES (?,?,?,?,?,?) ON CONFLICT(account_id, ticker) DO UPDATE SET
                   quantity=excluded.quantity, average_cost=excluded.average_cost,
                   realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at""",
                (self.account_id, fill.ticker, new_qty, new_avg, realized_total + realized, _iso(fill.timestamp)))
            c.execute("INSERT INTO cash_entries (account_id, ts, kind, amount, ref) VALUES (?,?,?,?,?)",
                      (self.account_id, _iso(fill.timestamp), "trade", cash_delta, fill_id))
            c.execute("UPDATE orders SET status=?, venue_order_id=COALESCE(?, venue_order_id), "
                      "venue_status=COALESCE(?, venue_status), updated_at=? WHERE order_id=?",
                      (final_status.value, fill.venue_order_id, venue_status, _iso(fill.timestamp), fill.order_id))
            self._audit(c, "fill_applied", {"fill_id": fill_id, **fill.model_dump(mode="json"),
                                            "realized_pnl": realized, "cash_delta": cash_delta}, fill.timestamp)
        return True

    def fills(self, limit: int = 5000) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM fills WHERE account_id=? ORDER BY ts LIMIT ?",
                                           (self.account_id, limit))]

    def record_dividend(self, ticker: str, ex_date: date, per_share: float, quantity: float, now: datetime) -> bool:
        if quantity <= 0 or per_share <= 0:
            return False
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO cash_entries (account_id, ts, kind, amount, ref) VALUES (?,?,?,?,?)",
                            (self.account_id, _iso(now), "dividend", per_share * quantity, f"{ticker}:{ex_date}"))
            return cur.rowcount == 1

    # ----------------------------------------------------------------- equity

    def record_snapshot(self, *, ts: datetime, session_date: date, cash: float, market_value: float,
                        prices: dict[str, float], kind: str) -> dict[str, float]:
        equity = cash + market_value
        with self.tx() as c:
            prev = c.execute("SELECT MAX(peak_equity) p FROM equity_snapshots WHERE account_id=?",
                             (self.account_id,)).fetchone()["p"]
            initial = c.execute("SELECT initial_capital FROM accounts WHERE account_id=?",
                                (self.account_id,)).fetchone()["initial_capital"]
            peak = max(prev or initial, equity)
            drawdown = 0.0 if peak <= 0 else (peak - equity) / peak
            c.execute("""INSERT INTO equity_snapshots (account_id, ts, session_date, cash, market_value, equity,
                         peak_equity, drawdown, kind, prices) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (self.account_id, _iso(ts), session_date.isoformat(), cash, market_value, equity, peak,
                       drawdown, kind, json.dumps(prices)))
        return {"equity": equity, "peak_equity": peak, "drawdown": drawdown}

    def snapshots(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            rows = self._all("SELECT * FROM equity_snapshots WHERE account_id=? AND kind=? ORDER BY ts",
                             (self.account_id, kind))
        else:
            rows = self._all("SELECT * FROM equity_snapshots WHERE account_id=? ORDER BY ts", (self.account_id,))
        return [dict(r) for r in rows]

    def peak_equity(self) -> float:
        row = self._one("SELECT MAX(peak_equity) p FROM equity_snapshots WHERE account_id=?", (self.account_id,))
        return float(row["p"]) if row and row["p"] is not None else float(self.account()["initial_capital"])

    # --------------------------------------------------------------- controls

    def set_control(self, key: str, value: dict[str, Any], actor: str, note: str = "", now: datetime | None = None) -> None:
        now = now or utcnow()
        with self.tx() as c:
            c.execute("""INSERT INTO controls VALUES (?,?,?,?,?,?) ON CONFLICT(account_id, key) DO UPDATE SET
                         value=excluded.value, actor=excluded.actor, note=excluded.note, updated_at=excluded.updated_at""",
                      (self.account_id, key, json.dumps(value), actor, note, _iso(now)))
            self._audit(c, f"control:{key}", {"value": value, "actor": actor, "note": note}, now)

    def control(self, key: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM controls WHERE account_id=? AND key=?", (self.account_id, key))
        if not row:
            return {"active": False}
        return {**json.loads(row["value"]), "actor": row["actor"], "note": row["note"], "updated_at": row["updated_at"]}

    # ------------------------------------------------------------------- runs

    def start_run(self, run_id: str, kind: str, session_date: date, now: datetime) -> dict[str, Any]:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO runs (run_id, account_id, kind, session_date, status, started_at) "
                      "VALUES (?,?,?,?,?,?)", (run_id, self.account_id, kind, session_date.isoformat(), "running",
                                                _iso(now)))
            c.execute("UPDATE runs SET status='running', error=NULL WHERE run_id=? AND status!='completed'", (run_id,))
            return dict(c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if not row:
            return None
        out = dict(row)
        out["steps"] = json.loads(out["steps"] or "{}")
        return out

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM runs WHERE account_id=? ORDER BY started_at DESC LIMIT ?",
                         (self.account_id, limit))
        return [{**dict(r), "steps": json.loads(r["steps"] or "{}")} for r in rows]

    def mark_step(self, run_id: str, step: str, payload: Any = None) -> None:
        with self.tx() as c:
            row = c.execute("SELECT steps FROM runs WHERE run_id=?", (run_id,)).fetchone()
            steps = json.loads(row["steps"] or "{}")
            steps[step] = {"done_at": _iso(utcnow()), "result": payload}
            c.execute("UPDATE runs SET steps=? WHERE run_id=?", (json.dumps(steps, default=str), run_id))

    def finish_run(self, run_id: str, status: str, error: str | None = None, now: datetime | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET status=?, error=?, finished_at=? WHERE run_id=?",
                      (status, error, _iso(now or utcnow()), run_id))

    # -------------------------------------------------------------- LLM usage

    def record_llm_usage(self, rows: list[dict[str, Any]], now: datetime) -> None:
        if not rows:
            return
        with self.tx() as c:
            c.executemany("""INSERT INTO llm_usage (account_id, run_id, ticker, role, model, input_tokens,
                             output_tokens, cost_usd, ts) VALUES (?,?,?,?,?,?,?,?,?)""",
                          [(self.account_id, r.get("run_id"), r.get("ticker"), r.get("role"), r.get("model"),
                            int(r["input_tokens"]), int(r["output_tokens"]), r.get("cost_usd"), _iso(now))
                           for r in rows])

    def llm_usage_summary(self, since: str | None = None) -> dict[str, Any]:
        sql = """SELECT model, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) c,
                 SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) unpriced, COUNT(*) n
                 FROM llm_usage WHERE account_id=?"""
        args: list[Any] = [self.account_id]
        if since:
            sql += " AND ts>=?"
            args.append(since)
        rows = self._all(sql + " GROUP BY model", tuple(args))
        by_model = {r["model"] or "unknown": {"input_tokens": r["i"], "output_tokens": r["o"],
                                              "cost_usd": r["c"], "unpriced_calls": r["unpriced"], "calls": r["n"]}
                    for r in rows}
        return {
            "by_model": by_model,
            "input_tokens": sum(v["input_tokens"] for v in by_model.values()),
            "output_tokens": sum(v["output_tokens"] for v in by_model.values()),
            "priced_cost_usd": sum(v["cost_usd"] or 0.0 for v in by_model.values()),
            "unpriced_calls": sum(v["unpriced_calls"] for v in by_model.values()),
        }

    # ------------------------------------------------------------ publication

    def enqueue_publication(self, fill_id: str, ticker: str, side: str, quantity: float, price: float, ts: str,
                            comment: str, now: datetime) -> None:
        with self.tx() as c:
            c.execute("""INSERT OR IGNORE INTO publication_queue (account_id, fill_id, ticker, side, quantity,
                         local_fill_price, local_fill_ts, comment, created_at, updated_at)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (self.account_id, fill_id, ticker, side, quantity, price, ts, comment, _iso(now), _iso(now)))

    def publication_entries(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._all("SELECT * FROM publication_queue WHERE account_id=? AND status=? ORDER BY entry_id",
                             (self.account_id, status))
        else:
            rows = self._all("SELECT * FROM publication_queue WHERE account_id=? ORDER BY entry_id", (self.account_id,))
        return [dict(r) for r in rows]

    def confirm_publication(self, entry_id: int, *, status: str, savvy_price: float | None, savvy_quantity: float | None,
                            savvy_ts: str | None, entered_by: str, note: str, now: datetime) -> dict[str, Any]:
        with self.tx() as c:
            row = c.execute("SELECT * FROM publication_queue WHERE entry_id=? AND account_id=?",
                            (entry_id, self.account_id)).fetchone()
            if not row:
                raise LedgerError(f"no publication entry {entry_id}")
            c.execute("""UPDATE publication_queue SET status=?, savvy_price=?, savvy_quantity=?, savvy_ts=?,
                         entered_by=?, note=?, updated_at=? WHERE entry_id=?""",
                      (status, savvy_price, savvy_quantity, savvy_ts, entered_by, note, _iso(now), entry_id))
            self._audit(c, "publication_confirmed", {"entry_id": entry_id, "status": status,
                                                     "savvy_price": savvy_price, "entered_by": entered_by}, now)
            return dict(c.execute("SELECT * FROM publication_queue WHERE entry_id=?", (entry_id,)).fetchone())

    # ----------------------------------------------------------------- audit

    def _audit(self, c: sqlite3.Connection, event: str, payload: dict[str, Any], now: datetime | None) -> None:
        c.execute("INSERT INTO audit_log (account_id, ts, event, payload) VALUES (?,?,?,?)",
                  (self.account_id, _iso(now or utcnow()), event, json.dumps(payload, default=str)))

    def audit(self, event: str, payload: dict[str, Any], now: datetime | None = None) -> None:
        with self.tx() as c:
            self._audit(c, event, payload, now)

    def audit_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM audit_log WHERE account_id=? ORDER BY event_id DESC LIMIT ?",
                                           (self.account_id, limit))]
