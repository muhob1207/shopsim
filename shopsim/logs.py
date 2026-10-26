"""Structured JSON logging (one JSON object per line).

The record shape is deliberately *flat* — no dotted keys, no nested objects —
because the same line is shipped unchanged to Loki, Elasticsearch and
OpenSearch, and Perftelion's Elasticsearch/OpenSearch querier reads
``_source`` fields by exact top-level key.  Levels are lower-case
(``debug|info|warn|error``) so Loki label matchers and keyword term queries
agree.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opentelemetry import trace

_LEVELS = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service: str, version: str, env: str, instance: str) -> None:
        super().__init__()
        self._static = {"service": service, "version": version, "env": env, "instance": instance}

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        doc: dict[str, Any] = {
            "@timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "level": _LEVELS.get(record.levelno, "info"),
            **self._static,
            "logger": record.name,
            "message": record.getMessage(),
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            doc["trace_id"] = format(ctx.trace_id, "032x")
            doc["span_id"] = format(ctx.span_id, "016x")
        fields = getattr(record, "fields", None)
        if fields:
            for key, value in fields.items():
                if value is not None and key not in doc:
                    doc[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            doc.setdefault("error_type", record.exc_info[0].__name__)
            doc["stack"] = self.formatException(record.exc_info)[-4000:]
        return json.dumps(doc, default=str, ensure_ascii=False)


def setup_logging(*, service: str, version: str, env: str, instance: str, log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_dir / f"{service}.log", encoding="utf-8")
    file_handler.setFormatter(JsonFormatter(service=service, version=version, env=env, instance=instance))
    root.addHandler(file_handler)

    # Human-readable warnings on stderr (captured by the start script).
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(console)

    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(f"shopsim.{service}")


def log(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log *message* with extra flat fields (``event``, ``duration_ms``, ...)."""
    logger.log(level, message, extra={"fields": fields})
