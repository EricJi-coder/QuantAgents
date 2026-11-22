"""Import pure-computation modules from the vendored QuantDinger backend.

``app/__init__.py`` in QuantDinger builds the Flask application (database pools, workers,
Kafka clients). The two modules reused here — ``app.services.market_schedule`` (exchange
calendar sessions) and ``app.services.backtest.metrics`` (benchmark-relative metrics) — need
only pandas and exchange_calendars, so they are loaded with namespace stubs for their parent
packages instead of running that factory.
"""

from __future__ import annotations

import importlib
import sys
import types
from functools import cache
from pathlib import Path

from aipt.config import ROOT

QUANTDINGER_BACKEND = ROOT / "vendor" / "QuantDinger" / "backend_api_python"


def _stub_package(name: str, path: Path) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module


@cache
def _prepare() -> None:
    if not QUANTDINGER_BACKEND.is_dir():
        raise RuntimeError(f"QuantDinger checkout not found at {QUANTDINGER_BACKEND}")
    app_dir = QUANTDINGER_BACKEND / "app"
    existing = sys.modules.get("app")
    if existing is not None and str(app_dir) not in list(getattr(existing, "__path__", [])):
        raise RuntimeError("another top-level 'app' package is already imported; cannot load QuantDinger modules")
    _stub_package("app", app_dir)
    _stub_package("app.services", app_dir / "services")
    _stub_package("app.services.backtest", app_dir / "services" / "backtest")


def market_schedule():
    _prepare()
    return importlib.import_module("app.services.market_schedule")


def backtest_metrics():
    _prepare()
    return importlib.import_module("app.services.backtest.metrics")
