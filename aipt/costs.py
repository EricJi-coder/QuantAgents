"""LLM token and API-cost tracking, kept separate from investment performance.

A LangChain callback (the same hook TradingAgents' CLI StatsCallbackHandler uses) records
input/output tokens per call with the model name and the graph node ("role") that made it.
Cost is computed only from prices the operator configures; an unpriced model is reported as
unpriced rather than estimated.
"""

from __future__ import annotations

import threading
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from aipt.config import PriceEntry


def price_call(prices: dict[str, PriceEntry], model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    if not model:
        return None
    entry = prices.get(model)
    if entry is None:
        # Providers often return a dated snapshot id ("claude-sonnet-5-20260101"); match a configured prefix.
        entry = next((p for name, p in prices.items() if model.startswith(name)), None)
    if entry is None:
        return None
    return (input_tokens * entry.input + output_tokens * entry.output) / 1_000_000


class UsageTracker(BaseCallbackHandler):
    def __init__(self, prices: dict[str, PriceEntry]):
        super().__init__()
        self.prices = prices
        self._lock = threading.Lock()
        self._models: dict[UUID, tuple[str | None, str | None]] = {}
        self.records: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}

    def set_context(self, **context: Any) -> None:
        with self._lock:
            self.context = dict(context)

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[Any]], *, run_id: UUID,
                            metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        params = kwargs.get("invocation_params") or {}
        model = params.get("model") or params.get("model_name") or (serialized.get("kwargs") or {}).get("model")
        role = (metadata or {}).get("langgraph_node")
        with self._lock:
            self._models[run_id] = (model, role)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        usage, model_from_response = None, None
        try:
            message = response.generations[0][0].message
            usage = getattr(message, "usage_metadata", None)
            meta = getattr(message, "response_metadata", None) or {}
            model_from_response = meta.get("model_name") or meta.get("model")
        except (IndexError, AttributeError, TypeError):
            pass
        with self._lock:
            model, role = self._models.pop(run_id, (None, None))
            model = model_from_response or model
            tokens_in = int((usage or {}).get("input_tokens", 0))
            tokens_out = int((usage or {}).get("output_tokens", 0))
            self.records.append({
                **self.context, "model": model, "role": role, "input_tokens": tokens_in,
                "output_tokens": tokens_out, "usage_reported": usage is not None,
                "cost_usd": price_call(self.prices, model, tokens_in, tokens_out),
            })

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            out, self.records = self.records, []
            return out
