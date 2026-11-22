"""Real-money trading is unreachable: configuration, venue and adapter each refuse it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aipt.config import load_settings
from aipt.execution.venues import QuantDingerClient, QuantDingerPaperVenue, RealTradingRefused, VenueError
from aipt.models import OrderSide


class FakeClient:
    def __init__(self, who: dict, order: dict | None = None):
        self.who, self.order, self.bodies = who, order or {}, []

    def whoami(self):
        return self.who

    def place_order(self, body, idempotency_key):
        self.bodies.append(body)
        return self.order, False


PAPER_WHO = {"paper_only": True, "scopes": ["R", "T"], "markets": ["USStock"]}


@pytest.mark.parametrize("override", [{"mode": "live"}, {"mode": "real"}, {"risk": {"max_gross_exposure": 1.5}}])
def test_config_cannot_enable_live_trading_or_leverage(override):
    with pytest.raises(ValidationError):
        load_settings(overrides=override)


def test_default_config_is_paper_with_required_limits():
    s = load_settings()
    assert s.mode == "paper" and s.account.initial_capital == 100_000.0 and s.account.base_currency == "USD"
    r = s.risk
    assert (r.max_position_weight, r.max_gross_exposure, r.min_cash_reserve, r.max_daily_buy_turnover,
            r.drawdown_halt, r.allow_fractional_shares) == (0.10, 1.0, 0.05, 0.20, 0.10, False)


def test_non_paper_token_is_refused_before_any_order():
    client = FakeClient({**PAPER_WHO, "paper_only": False})
    venue = QuantDingerPaperVenue(client)
    with pytest.raises(RealTradingRefused):
        venue.submit(idempotency_key="k", ticker="AAPL", side=OrderSide.BUY, quantity=1)
    assert client.bodies == []


def test_response_not_marked_paper_is_refused():
    venue = QuantDingerPaperVenue(FakeClient(PAPER_WHO, {"paper": False, "status": "filled", "fill_price": 1}))
    with pytest.raises(RealTradingRefused):
        venue.submit(idempotency_key="k", ticker="AAPL", side=OrderSide.BUY, quantity=1)


def test_order_body_never_carries_a_broker_credential():
    client = FakeClient(PAPER_WHO, {"paper": True, "status": "filled", "fill_price": 10.0, "qty": 1,
                                    "order_uid": "u1"})
    result = QuantDingerPaperVenue(client).submit(idempotency_key="k", ticker="AAPL", side=OrderSide.BUY, quantity=1)
    assert result.status == "filled" and result.reference_price == 10.0
    assert "credential_id" not in client.bodies[0]


def test_token_lacking_market_or_scope_is_refused():
    with pytest.raises(VenueError):
        QuantDingerPaperVenue(FakeClient({**PAPER_WHO, "markets": ["Crypto"]})).verify_paper_only()
    with pytest.raises(VenueError):
        QuantDingerPaperVenue(FakeClient({**PAPER_WHO, "scopes": ["R"]})).verify_paper_only()


def test_client_requires_an_agent_token():
    with pytest.raises(VenueError):
        QuantDingerClient("http://127.0.0.1:5055", "")
    with pytest.raises(VenueError):
        QuantDingerClient("http://127.0.0.1:5055", "some-admin-jwt")
