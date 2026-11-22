"""Structured (JSON lines) logging to a file, human-readable logging to stderr."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET = re.compile(r"(qd_agent_[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+|(sk-ant-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+")


def redact(text: str) -> str:
    return _SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "…", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": datetime.fromtimestamp(record.created, UTC).isoformat(), "level": record.levelname,
                   "logger": record.name, "msg": redact(record.getMessage())}
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_aipt_configured", False):
        return
    root.setLevel(level)
    file_handler = RotatingFileHandler(log_dir / "aipt.jsonl", maxBytes=20_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(_RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)
    root.addHandler(console)
    for noisy in ("httpx", "urllib3", "yfinance", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._aipt_configured = True  # type: ignore[attr-defined]
