"""FastAPI application factory shared by all shopsim services.

Every service gets the same operational surface:

* ``GET /health``  — liveness plus version, commit and applied resource limits
* ``GET /metrics`` — Prometheus exposition (see ``shopsim.metrics``)
* ``GET|PUT /admin/faults`` — runtime fault injection (see ``shopsim.faults``)
* ``GET /openapi.json`` — FastAPI's spec; the gateway's spec is what the
  Perftelion catalog "import from OpenAPI URL" test points at

and the same request pipeline: one SERVER span per request named after the
route template, a request-duration histogram, an in-flight gauge and a
(sampled) JSON access log.  5xx and slow requests are always logged.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import propagate
from opentelemetry.trace import SpanKind, Status, StatusCode

from shopsim import faults, metrics
from shopsim.clients import Downstream, DownstreamError
from shopsim.config import LabConfig, ServiceConfig
from shopsim.db import PoolTimeout
from shopsim.limits import AppliedLimits
from shopsim.logs import log
from shopsim.metrics import APP_ERRORS, HTTP_IN_FLIGHT, HTTP_SERVER
from shopsim.telemetry import shutdown_tracing, tracer

_UNTRACED_PREFIXES = ("/metrics", "/health", "/admin", "/openapi.json", "/docs", "/redoc", "/favicon.ico")


@dataclass
class ServiceContext:
    name: str
    version: str
    commit: str
    env: str
    cfg: ServiceConfig
    lab: LabConfig
    logger: logging.Logger
    limits: AppliedLimits
    started_at: str
    peers: dict[str, Downstream] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    def peer(self, name: str, *, timeout_sec: float | None = None) -> Downstream:
        if name not in self.peers:
            self.peers[name] = Downstream(
                name,
                self.lab.peer_url(name),
                timeout_sec=timeout_sec or self.lab.downstream_timeout_sec,
                logger=self.logger,
            )
        return self.peers[name]

    def data_file(self, filename: str) -> Path:
        return self.lab.data_dir / filename


Hook = Callable[[ServiceContext], Awaitable[None]]


@dataclass
class ServiceModule:
    router: APIRouter
    startup: Hook | None = None
    shutdown: Hook | None = None
    description: str = ""


class ObservabilityMiddleware:
    """Pure-ASGI middleware: tracing, RED metrics, access log, runtime faults."""

    def __init__(self, app, ctx: ServiceContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(_UNTRACED_PREFIXES):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        raw_path = scope.get("path", "")
        carrier = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        parent = propagate.extract(carrier)
        response = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
            await send(message)

        started = time.perf_counter()
        HTTP_IN_FLIGHT.inc()
        with tracer().start_as_current_span(
            f"{method} {raw_path}", context=parent, kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": raw_path},
        ) as span:
            error: BaseException | None = None
            try:
                await faults.apply()
                await self.app(scope, receive, send_wrapper)
            except faults.InjectedFault as exc:
                error = exc
                APP_ERRORS.labels("injected_fault").inc()
                await _send_json(send_wrapper, 500, {"error": str(exc)})
            except asyncio.CancelledError:
                response["status"] = 499  # client went away
                raise
            except BaseException as exc:  # noqa: BLE001 — re-raised for FastAPI's 500 handler
                error = exc
                response["status"] = 500
                raise
            finally:
                HTTP_IN_FLIGHT.dec()
                elapsed = time.perf_counter() - started
                route_obj = scope.get("route")
                # Unmatched requests share one label value so 404s/fault paths
                # cannot explode metric cardinality with raw ids.
                route = getattr(route_obj, "path", None) or "(unmatched)"
                status = int(response["status"])
                span.update_name(f"{method} {route if route_obj else raw_path}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                if status >= 500:
                    span.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
                if error is not None:
                    span.record_exception(error)
                HTTP_SERVER.labels(method, route, str(status)).observe(elapsed)
                self._access_log(method, route, raw_path, status, elapsed * 1000, error)

    def _access_log(self, method: str, route: str, path: str, status: int, ms: float, error: BaseException | None) -> None:
        fields = dict(
            event="http_request", http_method=method, http_route=route, url_path=path,
            http_status=status, duration_ms=round(ms, 1),
        )
        if status >= 500:
            if error is not None and not isinstance(error, faults.InjectedFault):
                self.ctx.logger.error(
                    f"{method} {route} failed with an unhandled {type(error).__name__} after {ms:.0f} ms",
                    exc_info=(type(error), error, error.__traceback__),
                    extra={"fields": fields},
                )
            else:
                log(self.ctx.logger, logging.ERROR, f"{method} {route} -> {status} in {ms:.0f} ms", **fields)
        elif ms >= self.ctx.lab.slow_request_ms:
            log(self.ctx.logger, logging.WARNING, f"slow request: {method} {route} -> {status} in {ms:.0f} ms",
                slow=True, **fields)
        elif random.random() < self.ctx.lab.access_log_sample_rate:
            log(self.ctx.logger, logging.INFO, f"{method} {route} -> {status} in {ms:.0f} ms", **fields)


async def _send_json(send, status: int, body: dict) -> None:
    import json

    payload = json.dumps(body).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
    })
    await send({"type": "http.response.body", "body": payload})


def create_app(ctx: ServiceContext, module: ServiceModule) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if module.startup:
            await module.startup(ctx)
        log(
            ctx.logger, logging.INFO,
            f"{ctx.name} {ctx.version} (commit {ctx.commit}) started on port {ctx.cfg.port}; "
            f"limits: cpu={ctx.limits.cpu_cores} cores, memory="
            f"{ctx.limits.as_dict()['memory_mb'] or 'unlimited'} MB, enforced={ctx.limits.enforced}",
            event="service_started", commit=ctx.commit, cpu_limit_cores=ctx.limits.cpu_cores,
            memory_limit_mb=ctx.limits.as_dict()["memory_mb"], limits_enforced=ctx.limits.enforced,
        )
        if not ctx.limits.enforced and (ctx.cfg.cpu_cores or ctx.cfg.memory_mb):
            log(ctx.logger, logging.WARNING, f"resource limits NOT enforced: {ctx.limits.detail}",
                event="limits_not_enforced")
        try:
            yield
        finally:
            log(ctx.logger, logging.INFO, f"{ctx.name} stopping", event="service_stopping")
            if module.shutdown:
                await module.shutdown(ctx)
            for client in ctx.peers.values():
                await client.close()
            shutdown_tracing()

    app = FastAPI(
        title=f"shopsim {ctx.name}",
        version=ctx.version,
        description=module.description,
        lifespan=lifespan,
    )
    app.add_middleware(ObservabilityMiddleware, ctx=ctx)

    @app.exception_handler(DownstreamError)
    async def _downstream(_request: Request, exc: DownstreamError):
        return JSONResponse({"error": str(exc), "peer": exc.peer}, status_code=exc.status)

    @app.exception_handler(PoolTimeout)
    async def _pool(_request: Request, exc: PoolTimeout):
        APP_ERRORS.labels("db_pool_exhausted").inc()
        return JSONResponse({"error": str(exc)}, status_code=503)

    @app.exception_handler(MemoryError)
    async def _oom(_request: Request, exc: MemoryError):
        APP_ERRORS.labels("out_of_memory").inc()
        log(ctx.logger, logging.ERROR,
            f"MemoryError: allocation failed — {ctx.name} is at its memory limit "
            f"({ctx.limits.as_dict()['memory_mb']} MB)", event="out_of_memory", error_type="MemoryError")
        return JSONResponse({"error": "out of memory"}, status_code=500)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {
            "status": "ok",
            "service": ctx.name,
            "version": ctx.version,
            "commit": ctx.commit,
            "environment": ctx.env,
            "startedAt": ctx.started_at,
            "limits": ctx.limits.as_dict(),
        }

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics():
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    @app.get("/admin/faults", include_in_schema=False)
    async def get_faults():
        return faults.FAULTS.as_dict()

    @app.put("/admin/faults", include_in_schema=False)
    async def put_faults(patch: faults.FaultsPatch):
        for key, value in patch.model_dump(exclude_none=True).items():
            setattr(faults.FAULTS, key, value)
        log(ctx.logger, logging.WARNING, f"runtime faults changed: {faults.FAULTS.as_dict()}",
            event="faults_changed", **{f"fault_{k}": v for k, v in faults.FAULTS.as_dict().items()})
        return faults.FAULTS.as_dict()

    app.include_router(module.router)
    return app
