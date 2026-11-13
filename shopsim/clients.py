"""Traced HTTP client for calls between shopsim services (aiohttp)."""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from opentelemetry import propagate
from opentelemetry.trace import SpanKind, Status, StatusCode

from shopsim.logs import log
from shopsim.metrics import APP_ERRORS, HTTP_CLIENT
from shopsim.telemetry import tracer

MAX_CONNECTIONS = 200


class DownstreamError(Exception):
    """A call to another service failed (transport error, timeout or 5xx)."""

    def __init__(self, peer: str, route: str, status: int, detail: str) -> None:
        super().__init__(f"{peer} {route}: {detail}")
        self.peer = peer
        self.route = route
        self.status = status  # status this service should answer with (502/503/504)
        self.detail = detail


class DownstreamResponse:
    """What callers read from a downstream response.

    The body is read inside :meth:`Downstream.call`, so the connection goes back
    to the pool at once, and callers keep the ``status_code``, ``text`` and
    ``json()`` they used with httpx.
    """

    __slots__ = ("status_code", "headers", "content", "_charset")

    def __init__(self, status_code: int, headers: Any, content: bytes, charset: str | None) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self._charset = charset

    @property
    def text(self) -> str:
        return self.content.decode(self._charset or "utf-8", errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.content)


class Downstream:
    def __init__(self, peer: str, base_url: str, *, timeout_sec: float, logger: logging.Logger) -> None:
        self.peer = peer
        self._logger = logger
        self._base_url = base_url.rstrip("/")
        parts = urlsplit(base_url)
        self._address = parts.hostname or ""
        self._port = parts.port or 80
        self._timeout_sec = timeout_sec
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        # Created on first use: an aiohttp session belongs to the running event
        # loop, and services build their peers before uvicorn starts it.
        if self._session is None:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS),
                timeout=aiohttp.ClientTimeout(total=self._timeout_sec),
                # Peers are always on loopback: never route through a proxy from the environment.
                trust_env=False,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def call(
        self,
        method: str,
        route: str,
        *,
        path: str | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
        raise_for_5xx: bool = True,
    ) -> DownstreamResponse:
        """Call *route* (a template such as ``/stock/{sku}``) on the peer.

        ``path`` is the concrete URL path; it defaults to the route.  4xx
        responses are returned to the caller; transport errors, timeouts and
        (by default) 5xx responses raise :class:`DownstreamError`.
        """
        url_path = path or route
        headers: dict[str, str] = {}
        with tracer().start_as_current_span(
            f"{method} {route}",
            kind=SpanKind.CLIENT,
            attributes={
                "http.request.method": method,
                "http.route": route,
                "url.path": url_path,
                "peer.service": self.peer,
                "server.address": self._address,
                "server.port": self._port,
            },
        ) as span:
            propagate.inject(headers)
            started = time.perf_counter()
            status = "error"
            request_kwargs: dict[str, Any] = {"json": json, "params": params, "headers": headers}
            if timeout_sec:
                request_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout_sec)
            try:
                async with self._get_session().request(method, self._base_url + url_path, **request_kwargs) as raw:
                    resp = DownstreamResponse(raw.status, raw.headers, await raw.read(), raw.charset)
                status = str(resp.status_code)
            except asyncio.TimeoutError as exc:  # aiohttp's own timeout errors subclass it
                status = "timeout"
                elapsed_ms = (time.perf_counter() - started) * 1000
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, "timeout"))
                APP_ERRORS.labels("downstream_timeout").inc()
                log(
                    self._logger, logging.ERROR,
                    f"call to {self.peer} {method} {route} timed out after {elapsed_ms:.0f} ms",
                    event="downstream_timeout", peer_service=self.peer, http_method=method,
                    http_route=route, duration_ms=round(elapsed_ms, 1), error_type=type(exc).__name__,
                )
                raise DownstreamError(self.peer, route, 504, f"timed out after {elapsed_ms:.0f} ms") from exc
            except aiohttp.ClientError as exc:
                status = "transport_error"
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                APP_ERRORS.labels("downstream_unavailable").inc()
                log(
                    self._logger, logging.ERROR,
                    f"call to {self.peer} {method} {route} failed: {exc!r}",
                    event="downstream_unavailable", peer_service=self.peer, http_method=method,
                    http_route=route, error_type=type(exc).__name__,
                )
                raise DownstreamError(self.peer, route, 502, f"unavailable: {exc!r}") from exc
            finally:
                HTTP_CLIENT.labels(self.peer, method, route, status).observe(time.perf_counter() - started)

            span.set_attribute("http.response.status_code", resp.status_code)
            if resp.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR, f"HTTP {resp.status_code}"))
                if raise_for_5xx:
                    detail = _error_detail(resp)
                    log(
                        self._logger, logging.ERROR,
                        f"{self.peer} {method} {route} answered HTTP {resp.status_code}: {detail}",
                        event="downstream_error", peer_service=self.peer, http_method=method,
                        http_route=route, http_status=resp.status_code,
                    )
                    APP_ERRORS.labels("downstream_5xx").inc()
                    # Pass gateway-style errors through unchanged; anything else is a bad gateway.
                    upstream = resp.status_code if resp.status_code in (502, 503, 504) else 502
                    raise DownstreamError(self.peer, route, upstream, f"HTTP {resp.status_code}: {detail}")
            return resp


def _error_detail(resp: DownstreamResponse) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("error") or body.get("detail") or body)[:300]
    except ValueError:
        pass
    return resp.text[:300]
