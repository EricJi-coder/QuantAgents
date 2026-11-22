"""Read-only checks against a running local QuantDinger (scripts/quantdinger_local.sh start).

Run with ``pytest -m quantdinger``. Skipped unless QUANTDINGER_BASE_URL and QUANTDINGER_AGENT_TOKEN are set and the gateway
answers. No orders are placed here: an order would land in QuantDinger's paper records and
show up in the live account's reconciliation as unknown to the ledger.
"""

from __future__ import annotations

import os

import pytest
import requests

from aipt.clock import SystemClock
from aipt.config import ROOT, load_dotenv_file
from aipt.execution.venues import QuantDingerClient, QuantDingerPaperVenue
from aipt.market.data import QuantDingerMarketData, YahooMarketData

load_dotenv_file(ROOT / ".env")
BASE, TOKEN = os.getenv("QUANTDINGER_BASE_URL"), os.getenv("QUANTDINGER_AGENT_TOKEN")


def _reachable() -> bool:
    if not (BASE and TOKEN):
        return False
    try:
        return requests.get(BASE.rstrip("/") + "/api/agent/v1/health", timeout=3).ok
    except requests.RequestException:
        return False


pytestmark = [pytest.mark.quantdinger,
              pytest.mark.skipif(not _reachable(), reason="local QuantDinger gateway not running/configured")]


@pytest.fixture(scope="module")
def client() -> QuantDingerClient:
    return QuantDingerClient(BASE, TOKEN)


def test_token_is_paper_only_and_scoped(client):
    who = QuantDingerPaperVenue(client).verify_paper_only()
    assert who["paper_only"] is True and "T" in who["scopes"] and "USStock" in who["markets"]
    assert who["max_order_notional"] >= 10_000  # one full 10% position of a $100k account


def test_quote_from_gateway_is_timestamped(client):
    clock = SystemClock()
    md = QuantDingerMarketData(client, clock, fallback=YahooMarketData(clock))
    q = md.quote("SPY")
    assert q.price > 0 and q.timestamp.tzinfo is not None and q.timestamp <= clock.now()


def test_paper_order_list_is_readable(client):
    assert isinstance(client.paper_orders(), list)
