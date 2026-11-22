"""Trading-universe gate: US-listed common stocks and ETFs only.

Fails closed: an instrument whose type or listing cannot be confirmed is not tradeable.
"""

from __future__ import annotations

import re

from aipt.config import RiskConfig
from aipt.models import InstrumentInfo

# A US ticker: 1-5 letters, optionally a share-class suffix (BRK.B / BRK-B). Anything with an
# exchange suffix (.L, .TO), a currency pair (-USD), an index (^) or futures (=F) is rejected.
_US_TICKER = re.compile(r"^[A-Z]{1,5}([.-][A-Z])?$")

# Yahoo exchange codes for US venues.
US_EXCHANGES = {"NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "BTS", "BATS", "NAS", "NYS", "PNK"}
ALLOWED_TYPES = {"EQUITY", "ETF"}


def check_instrument(ticker: str, info: InstrumentInfo | None, risk: RiskConfig) -> list[str]:
    """Reasons the instrument is outside the universe; empty when it is allowed."""
    reasons = []
    symbol = ticker.strip().upper()
    if not _US_TICKER.match(symbol):
        reasons.append(f"{ticker!r} is not a US-listed stock/ETF symbol")
        return reasons
    if symbol in risk.denied_tickers:
        reasons.append(f"{symbol} is on the denied list (leveraged/inverse product)")
    if info is None or not info.quote_type:
        reasons.append(f"cannot confirm {symbol}'s instrument type; refusing to trade it")
        return reasons
    if info.quote_type.upper() not in ALLOWED_TYPES:
        reasons.append(f"{symbol} is {info.quote_type}, not a stock or ETF")
    if info.exchange and info.exchange.upper() not in US_EXCHANGES:
        reasons.append(f"{symbol} is listed on {info.exchange}, not a US exchange")
    if info.exchange and info.exchange.upper() == "PNK":
        reasons.append(f"{symbol} trades OTC (Pink sheets), not on a US exchange")
    if info.currency and info.currency.upper() != "USD":
        reasons.append(f"{symbol} is quoted in {info.currency}, not USD")
    name = (info.name or "").lower()
    for pattern in risk.denied_name_patterns:
        if pattern.lower() in name:
            reasons.append(f"{symbol} ({info.name}) matches denied pattern {pattern!r} (leveraged/inverse)")
            break
    return reasons
