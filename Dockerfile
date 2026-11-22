# aipt: research + paper-execution adapter + scheduler. QuantDinger runs separately
# (scripts/quantdinger_local.sh natively, or QuantDinger's own docker compose).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY vendor/TradingAgents /app/vendor/TradingAgents
COPY vendor/QuantDinger/backend_api_python/app/services/market_schedule.py \
     /app/vendor/QuantDinger/backend_api_python/app/services/market_schedule.py
COPY vendor/QuantDinger/backend_api_python/app/services/backtest/metrics.py \
     /app/vendor/QuantDinger/backend_api_python/app/services/backtest/metrics.py
COPY pyproject.toml /app/
COPY aipt /app/aipt
COPY config /app/config

RUN pip install /app/vendor/TradingAgents && pip install -e /app \
    && useradd --create-home --uid 10001 aipt && mkdir -p /app/data /app/reports \
    && chown -R aipt /app/data /app/reports

USER aipt
VOLUME ["/app/data", "/app/reports"]
CMD ["aipt", "scheduler"]
