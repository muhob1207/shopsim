"""Traced HTTP client for calls between shopsim services."""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from opentelemetry import propagate
from opentelemetry.trace import SpanKind, Status, StatusCode

from shopsim.logs import log
from shopsim.metrics import APP_ERRORS, HTTP_CLIENT
from shopsim.telemetry import tracer


class DownstreamError(Exception):
    """A call to another service failed (transport error, timeout or 5xx)."""

    def __init__(self, peer: str, route: str, status: int, detail: str) -> None:
        super().__init__(f"{peer} {route}: {detail}")
        self.peer = peer
        self.route = route
        self.status = status  # status this service should answer with (502/503/504)
        self.detail = detail


class Downstream:
    def __init__(self, peer: str, base_url: str, *, timeout_sec: float, logger: logging.Logger) -> None:
        self.peer = peer
        self._logger = logger
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_sec),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            # Peers are always on loopback. Without this, httpx would also pick up a proxy from
            # HTTP(S)_PROXY or, on Windows, from the system (registry) proxy settings.
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

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
    ) -> httpx.Response:
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
                "server.address": self._client.base_url.host,
                "server.port": self._client.base_url.port or 80,
            },
        ) as span:
            propagate.inject(headers)
            started = time.perf_counter()
            status = "error"
            try:
                resp = await self._client.request(
                    method, url_path, json=json, params=params, headers=headers,
                    timeout=httpx.Timeout(timeout_sec) if timeout_sec else httpx.USE_CLIENT_DEFAULT,
                )
                status = str(resp.status_code)
            except httpx.TimeoutException as exc:
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
            except httpx.HTTPError as exc:
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


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("error") or body.get("detail") or body)[:300]
    except ValueError:
        pass
    return resp.text[:300]
