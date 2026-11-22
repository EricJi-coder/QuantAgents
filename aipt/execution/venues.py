"""Paper execution venues.

``QuantDingerPaperVenue`` is the production venue: QuantDinger's Agent Gateway
``POST /api/agent/v1/quick-trade/orders`` with a paper-only agent token. QuantDinger records
the order in ``qd_agent_paper_orders`` and fills a market order at its latest 1-minute close;
the gateway's ``Idempotency-Key`` makes resubmission after a crash return the original result.

``LocalPaperVenue`` emulates exactly that contract offline (own SQLite file, idempotent by
key, fills at a supplied reference price) for tests, replays and backtests.

Neither venue can reach a real broker: the QuantDinger venue refuses tokens that are not
paper-only, never sends ``credential_id``, and treats any response without ``paper: true``
as a safety failure.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests

from aipt.models import OrderSide, Quote, VenueOrderResult, utcnow

logger = logging.getLogger(__name__)


class VenueError(RuntimeError):
    pass


class RealTradingRefused(VenueError):
    """Raised when anything indicates a non-paper execution path."""


class PaperVenue(Protocol):
    name: str

    def submit(self, *, idempotency_key: str, ticker: str, side: OrderSide, quantity: float) -> VenueOrderResult: ...
    def list_orders(self) -> list[dict[str, Any]]: ...
    def emergency_stop(self, idempotency_key: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# QuantDinger Agent Gateway
# ---------------------------------------------------------------------------


class QuantDingerClient:
    """Minimal client for the documented Agent Gateway (docs/agent/agent-openapi.json)."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0, max_attempts: int = 4):
        if not token or not token.startswith("qd_agent_"):
            raise VenueError("QUANTDINGER_AGENT_TOKEN is missing or not an agent token")
        self.base = base_url.rstrip("/") + "/api/agent/v1"
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout
        self.max_attempts = max_attempts

    def _request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                 idempotency_key: str | None = None) -> tuple[dict[str, Any], requests.Response]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._session.request(method, self.base + path, params=params, json=json, headers=headers,
                                             timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(min(2 ** attempt, 10))
                continue
            retriable = resp.status_code in (429, 502, 503, 504) or (
                resp.status_code == 409 and "in progress" in resp.text)
            if retriable and attempt < self.max_attempts:
                time.sleep(float(resp.headers.get("Retry-After") or min(2 ** attempt, 10)))
                continue
            try:
                body = resp.json()
            except ValueError as exc:
                raise VenueError(f"QuantDinger {method} {path} returned non-JSON HTTP {resp.status_code}") from exc
            if resp.status_code >= 400 or body.get("code") not in (0, None):
                raise VenueError(f"QuantDinger {method} {path} failed: HTTP {resp.status_code} {body.get('message')}")
            return body, resp
        raise VenueError(f"QuantDinger {method} {path} unreachable: {last_exc}")

    def health(self) -> dict[str, Any]:
        resp = self._session.get(self.base + "/health", timeout=self.timeout)
        return resp.json()

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", "/whoami")[0]["data"]

    def klines(self, market: str, symbol: str, timeframe: str, limit: int = 1) -> list[dict[str, Any]]:
        data = self._request("GET", "/klines", params={"market": market, "symbol": symbol, "timeframe": timeframe,
                                                       "limit": limit})[0]["data"]
        return data.get("klines") or []

    def place_order(self, body: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool]:
        payload, resp = self._request("POST", "/quick-trade/orders", json=body, idempotency_key=idempotency_key)
        replayed = resp.headers.get("Idempotent-Replayed") == "true"
        data = payload.get("data") or {}
        if data.get("duplicate"):
            replayed, data = True, data.get("previous") or {}
        return data, replayed

    def paper_orders(self) -> list[dict[str, Any]]:
        return self._request("GET", "/portfolio/paper-orders")[0]["data"] or []

    def kill_switch(self, idempotency_key: str) -> dict[str, Any]:
        return self._request("POST", "/quick-trade/kill-switch", json={"confirm": True},
                             idempotency_key=idempotency_key)[0]["data"]


class QuantDingerPaperVenue:
    name = "quantdinger"

    def __init__(self, client: QuantDingerClient, market: str = "USStock"):
        self.client = client
        self.market = market
        self._verified = False

    def verify_paper_only(self) -> dict[str, Any]:
        who = self.client.whoami()
        if who.get("paper_only") is not True:
            raise RealTradingRefused("QuantDinger agent token is not paper_only; refusing to use it")
        if "T" not in (who.get("scopes") or []):
            raise VenueError("QuantDinger agent token lacks the T (trading) scope needed for paper orders")
        markets = who.get("markets") or []
        if "*" not in markets and self.market not in markets:
            raise VenueError(f"QuantDinger agent token does not allow market {self.market}: {markets}")
        self._verified = True
        return who

    def submit(self, *, idempotency_key: str, ticker: str, side: OrderSide, quantity: float) -> VenueOrderResult:
        if not self._verified:
            self.verify_paper_only()
        body = {"market": self.market, "symbol": ticker, "side": side.value, "qty": quantity, "order_type": "market"}
        data, replayed = self.client.place_order(body, idempotency_key)
        if data.get("paper") is not True:
            raise RealTradingRefused(f"QuantDinger response for {idempotency_key} is not a paper order: {data}")
        status = str(data.get("status") or "error")
        fill_price = data.get("fill_price")
        return VenueOrderResult(
            venue=self.name, venue_order_id=data.get("order_uid"), status=status,
            reference_price=float(fill_price) if fill_price is not None else None,
            filled_quantity=float(data.get("qty") or 0) if status == "filled" else 0.0,
            timestamp=utcnow(), replayed=replayed, note=str(data.get("note") or ""), raw=data)

    def list_orders(self) -> list[dict[str, Any]]:
        return self.client.paper_orders()

    def emergency_stop(self, idempotency_key: str) -> dict[str, Any]:
        return self.client.kill_switch(idempotency_key)


# ---------------------------------------------------------------------------
# Offline emulation of the same paper contract
# ---------------------------------------------------------------------------


class LocalPaperVenue:
    """Idempotent paper venue in its own SQLite file, mirroring QuantDinger's paper semantics.

    ``reference_price(ticker, side)`` returns the Quote the venue fills a market order at:
    a fresh quote in live use, or the next traded price after submission in a replay.
    """

    name = "local"

    def __init__(self, path: Path | str, reference_price: Callable[[str, OrderSide], Quote]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.reference_price = reference_price
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS venue_orders (
            idempotency_key TEXT PRIMARY KEY, order_uid TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
            qty REAL NOT NULL, fill_price REAL, status TEXT NOT NULL, note TEXT, price_ts TEXT, created_at TEXT NOT NULL)""")
        self.fail_next: str | None = None   # test hook: "error" raises, "reject" records a rejection

    def submit(self, *, idempotency_key: str, ticker: str, side: OrderSide, quantity: float) -> VenueOrderResult:
        row = self._conn.execute("SELECT * FROM venue_orders WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if row:
            return self._result(row, replayed=True)
        if self.fail_next == "error":
            self.fail_next = None
            raise VenueError("simulated venue outage")
        status, note, price, price_ts = "filled", "", None, None
        if self.fail_next == "reject":
            self.fail_next = None
            status, note = "rejected", "no last price available; recorded without fill"
        else:
            try:
                quote = self.reference_price(ticker, side)
                price, price_ts = quote.price, quote.timestamp.isoformat()
            except Exception as exc:
                status, note = "rejected", f"no last price available: {exc}"
        self._conn.execute("INSERT INTO venue_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (idempotency_key, uuid.uuid4().hex, ticker, side.value, quantity, price, status, note,
                            price_ts, datetime.now(UTC).isoformat()))
        row = self._conn.execute("SELECT * FROM venue_orders WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return self._result(row, replayed=False)

    def lookup(self, idempotency_key: str) -> bool:
        """Whether this venue has already received the order with this key."""
        return self._conn.execute("SELECT 1 FROM venue_orders WHERE idempotency_key=?",
                                  (idempotency_key,)).fetchone() is not None

    def _result(self, row: sqlite3.Row, replayed: bool) -> VenueOrderResult:
        ts = datetime.fromisoformat(row["price_ts"]) if row["price_ts"] else utcnow()
        return VenueOrderResult(venue=self.name, venue_order_id=row["order_uid"], status=row["status"],
                                reference_price=row["fill_price"],
                                filled_quantity=row["qty"] if row["status"] == "filled" else 0.0,
                                timestamp=ts, replayed=replayed, note=row["note"] or "",
                                raw={"symbol": row["symbol"], "side": row["side"], "qty": row["qty"], "paper": True})

    def list_orders(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM venue_orders ORDER BY created_at").fetchall()
        return [{"order_uid": r["order_uid"], "symbol": r["symbol"], "side": r["side"], "qty": r["qty"],
                 "fill_price": r["fill_price"], "status": r["status"], "note": r["note"]} for r in rows]

    def emergency_stop(self, idempotency_key: str) -> dict[str, Any]:
        # Market orders fill or reject immediately, so nothing is ever left open here.
        return {"cancelled_open_paper_orders": 0, "venue": self.name}
