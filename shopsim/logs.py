"""Structured JSON logging (one JSON object per line).

The record shape is deliberately *flat* — no dotted keys, no nested objects —
because the same line is shipped unchanged to Loki, Elasticsearch and
OpenSearch, and Perftelion's Elasticsearch/OpenSearch querier reads
``_source`` fields by exact top-level key.  Levels are lower-case
(``debug|info|warn|error``) so Loki label matchers and keyword term queries
agree.

Writes happen on a background thread.  Every service runs one asyncio event
loop, and a synchronous file or stderr write on that loop stalls every
in-flight request.  The root logger therefore only puts records on a queue,
and a ``QueueListener`` thread writes them.  Lines are still *formatted* on
the calling thread: the JSON line carries the active span's trace and span
ids, and the span context is only visible there.
"""
from __future__ import annotations

import atexit
import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from queue import SimpleQueue
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


class _PreformattingQueueHandler(QueueHandler):
    """Formats each record on the calling thread and queues the finished lines.

    ``QueueHandler.prepare`` formats with this handler's formatter (the JSON
    line) and then drops ``exc_info``, so the human-readable stderr line,
    traceback included, is rendered here first for records that reach stderr.
    """

    def __init__(self, log_queue: SimpleQueue, *, console_formatter: logging.Formatter, console_level: int) -> None:
        super().__init__(log_queue)
        self._console_formatter = console_formatter
        self._console_level = console_level

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        console_line = (
            self._console_formatter.format(record) if record.levelno >= self._console_level else ""
        )
        prepared = super().prepare(record)
        prepared.console_line = console_line
        return prepared


def setup_logging(*, service: str, version: str, env: str, instance: str, log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    # Both handlers run on the listener thread and write lines that are already finished.
    file_handler = logging.FileHandler(log_dir / f"{service}.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    # Human-readable warnings on stderr (captured by the start script).
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(console_line)s"))

    log_queue: SimpleQueue = SimpleQueue()
    queue_handler = _PreformattingQueueHandler(
        log_queue,
        console_formatter=logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"),
        console_level=logging.WARNING,
    )
    queue_handler.setFormatter(JsonFormatter(service=service, version=version, env=env, instance=instance))
    root.addHandler(queue_handler)

    listener = QueueListener(log_queue, file_handler, console, respect_handler_level=True)
    listener.start()
    # Drains the queue on a normal interpreter exit.  A forced kill (Stop-Lab's
    # taskkill /F) loses only the records queued in the last few milliseconds.
    atexit.register(listener.stop)

    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(f"shopsim.{service}")


def log(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log *message* with extra flat fields (``event``, ``duration_ms``, ...)."""
    logger.log(level, message, extra={"fields": fields})
