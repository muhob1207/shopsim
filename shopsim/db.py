"""SQLite-backed "database server" with a bounded connection pool.

SQLite runs in-process, so on its own it would never look like a remote
database.  Two things make it behave like one for performance testing:

* every statement first pays a simulated network round trip
  (``db_latency_ms`` in the lab config), spent *while holding* a pooled
  connection — exactly like a real client waiting on a database server;
* connections come from a fixed-size pool; when it is exhausted callers queue,
  which is visible as ``sut_db_pool_wait_seconds``, a ``db.pool.acquire`` span
  and a ``db_pool_wait_high`` warning log.

Statements run on a dedicated thread pool sized to the connection pool, so the
default asyncio executor size can never become a hidden second limit.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from opentelemetry.trace import SpanKind, Status, StatusCode

from shopsim.logs import log
from shopsim.metrics import DB_POOL_IN_USE, DB_POOL_SIZE, DB_POOL_WAIT, DB_POOL_WAITING, DB_QUERY
from shopsim.telemetry import tracer


class PoolTimeout(Exception):
    """No database connection became free within the acquire timeout."""


class Database:
    def __init__(
        self,
        *,
        name: str,
        path: Path,
        pool_size: int,
        latency_ms: float,
        acquire_timeout_sec: float = 2.0,
        wait_warn_ms: float = 100.0,
        logger: logging.Logger,
    ) -> None:
        self.name = name
        self.path = path
        self.pool_size = pool_size
        self._latency = max(0.0, latency_ms) / 1000.0
        self._acquire_timeout = acquire_timeout_sec
        self._wait_warn_ms = wait_warn_ms
        self._logger = logger
        self._pool: asyncio.Queue[sqlite3.Connection] = asyncio.Queue()
        self._executor = ThreadPoolExecutor(max_workers=pool_size + 2, thread_name_prefix=f"db-{name}")
        self._in_use = 0
        self._waiting = 0

    # -- lifecycle ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    async def start(self, schema_and_seed: Callable[[sqlite3.Connection], None]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        setup = self._connect()
        try:
            schema_and_seed(setup)
        finally:
            setup.close()
        for _ in range(self.pool_size):
            self._pool.put_nowait(self._connect())
        DB_POOL_SIZE.set(self.pool_size)

    async def close(self) -> None:
        while not self._pool.empty():
            self._pool.get_nowait().close()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # -- pool --------------------------------------------------------------

    async def _acquire(self) -> sqlite3.Connection:
        """Take a connection, or raise ``asyncio.TimeoutError`` after the acquire timeout.

        Not ``asyncio.wait_for(queue.get())``: on Python 3.10/3.11 a get that
        completes at the same moment as the timeout loses the item, which would
        silently shrink the pool during exactly the tests that stress it.
        """
        getter = asyncio.ensure_future(self._pool.get())
        try:
            done, _ = await asyncio.wait({getter}, timeout=self._acquire_timeout)
        except BaseException:
            # Cancelled while waiting: hand back a connection the getter may already hold.
            if getter.done() and not getter.cancelled() and getter.exception() is None:
                self._pool.put_nowait(getter.result())
            else:
                getter.cancel()
            raise
        if getter not in done:
            getter.cancel()  # still pending, so the item (if any) stays in the queue
            raise asyncio.TimeoutError
        return getter.result()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator["Session"]:
        started = time.perf_counter()
        self._waiting += 1
        DB_POOL_WAITING.set(self._waiting)
        with tracer().start_as_current_span(
            "db.pool.acquire",
            attributes={"db.system": "sqlite", "db.name": self.name, "db.pool.size": self.pool_size},
        ) as span:
            try:
                conn = await self._acquire()
            except asyncio.TimeoutError:
                span.set_status(Status(StatusCode.ERROR, "pool exhausted"))
                waited_ms = (time.perf_counter() - started) * 1000
                log(
                    self._logger, logging.ERROR,
                    f"database connection pool exhausted: no connection after {waited_ms:.0f} ms "
                    f"(pool size {self.pool_size}, {self._in_use} in use, {self._waiting} waiting)",
                    event="db_pool_exhausted", db_name=self.name, pool_size=self.pool_size,
                    pool_in_use=self._in_use, pool_waiting=self._waiting, wait_ms=round(waited_ms, 1),
                )
                raise PoolTimeout(f"{self.name}: connection pool exhausted after {waited_ms:.0f} ms") from None
            finally:
                self._waiting -= 1
                DB_POOL_WAITING.set(self._waiting)
            waited = time.perf_counter() - started
            span.set_attribute("db.pool.wait_ms", round(waited * 1000, 2))
        DB_POOL_WAIT.observe(waited)
        if waited * 1000 >= self._wait_warn_ms:
            log(
                self._logger, logging.WARNING,
                f"waited {waited * 1000:.0f} ms for a database connection "
                f"(pool size {self.pool_size}, {self._in_use} in use, {self._waiting} waiting)",
                event="db_pool_wait_high", db_name=self.name, pool_size=self.pool_size,
                pool_in_use=self._in_use, pool_waiting=self._waiting, wait_ms=round(waited * 1000, 1),
            )
        self._in_use += 1
        DB_POOL_IN_USE.set(self._in_use)
        try:
            yield Session(self, conn)
        finally:
            self._in_use -= 1
            DB_POOL_IN_USE.set(self._in_use)
            self._pool.put_nowait(conn)

    # -- statements --------------------------------------------------------

    async def _execute(self, conn: sqlite3.Connection, operation: str, sql: str, params: tuple, fetch: str) -> Any:
        def _run() -> Any:
            if self._latency:
                time.sleep(self._latency)  # simulated round trip to the database server
            cur = conn.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount

        with tracer().start_as_current_span(
            operation,
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": "sqlite",
                "db.name": self.name,
                "db.operation": operation,
                "db.statement": sql,
                "peer.service": self.name,
            },
        ) as span:
            started = time.perf_counter()
            try:
                return await asyncio.get_running_loop().run_in_executor(self._executor, _run)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                DB_QUERY.labels(operation).observe(time.perf_counter() - started)


class Session:
    """A checked-out connection; every call is one traced statement."""

    def __init__(self, db: Database, conn: sqlite3.Connection) -> None:
        self._db = db
        self._conn = conn

    async def fetch_one(self, operation: str, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return await self._db._execute(self._conn, operation, sql, params, "one")

    async def fetch_all(self, operation: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return await self._db._execute(self._conn, operation, sql, params, "all")

    async def execute(self, operation: str, sql: str, params: tuple = ()) -> int:
        return await self._db._execute(self._conn, operation, sql, params, "rowcount")
