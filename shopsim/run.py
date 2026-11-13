"""Start one shopsim service.

    python -m shopsim.run --service inventory --config C:\\perftelion-testing\\sut\\shopsim.json

Order matters: resource limits are applied first (so everything the service
allocates is counted against them), then logging, tracing and metrics, then
the web server.

A service can run as several worker processes (``workers`` in its lab config,
else ``_DEFAULT_WORKERS``).  CPython executes bytecode on one core per process,
so a CPU-bound service needs processes, not threads, to use more than one
core.  With several workers the parent applies the limits and only
supervises: the workers inherit its Job Object, so they share one CPU cap, and
each builds its own app through :func:`create_worker_app`.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from shopsim.config import REPO_ROOT, SERVICE_NAMES, LabConfig, ServiceConfig, load_config, load_versions
from shopsim.limits import AppliedLimits, apply_limits

# Worker processes per service when the lab config sets no ``workers``.
_DEFAULT_WORKERS = {"gateway": 2}

# How the parent hands the service to its workers (spawned processes inherit the environment).
_ENV_SERVICE = "SHOPSIM_SERVICE"
_ENV_CONFIG = "SHOPSIM_CONFIG"
_ENV_LIMITS = "SHOPSIM_LIMITS"


def _git_commit() -> str:
    commit = os.environ.get("SHOPSIM_GIT_COMMIT")
    if commit:
        return commit[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _workers(svc: ServiceConfig) -> int:
    return max(1, int(svc.get("workers") or _DEFAULT_WORKERS.get(svc.name, 1)))


def _build_app(lab: LabConfig, svc: ServiceConfig, limits: AppliedLimits, *, worker: bool):
    # Imported after the limits are in place.
    from shopsim import metrics, services
    from shopsim.logs import setup_logging
    from shopsim.telemetry import setup_tracing
    from shopsim.web import ServiceContext, create_app

    version = load_versions().get(svc.name, "0.0.0")
    commit = _git_commit()
    instance = f"{socket.gethostname()}:{svc.port}"
    log_file = f"{svc.name}.log"
    if worker:
        # One log file per worker: several processes appending to one file on
        # Windows can overwrite each other's lines.
        instance = f"{instance}/{os.getpid()}"
        log_file = f"{svc.name}-{os.getpid()}.log"
    logger = setup_logging(service=svc.name, version=version, env=lab.environment, instance=instance,
                           log_dir=lab.log_dir, file_name=log_file)
    setup_tracing(service=svc.name, version=version, env=lab.environment, instance=instance,
                  endpoint=lab.otlp_traces_endpoint)
    metrics.install(limits, service=svc.name, version=version, env=lab.environment, commit=commit)
    lab.data_dir.mkdir(parents=True, exist_ok=True)

    ctx = ServiceContext(
        name=svc.name,
        version=version,
        commit=commit,
        env=lab.environment,
        cfg=svc,
        lab=lab,
        logger=logger,
        limits=limits,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    module = services.load(svc.name).create(ctx)
    return create_app(ctx, module)


def create_worker_app():
    """App factory run in each worker process (``uvicorn.run(..., factory=True)``)."""
    lab = load_config(os.environ[_ENV_CONFIG])
    svc = lab.service(os.environ[_ENV_SERVICE])
    # The limits were applied by the parent; the worker only reports them.
    limits = AppliedLimits(**json.loads(os.environ[_ENV_LIMITS]))
    return _build_app(lab, svc, limits, worker=True)


def _prepare_multiprocess_metrics(lab: LabConfig, svc: ServiceConfig) -> None:
    """Point prometheus_client's multi-process mode at an empty directory.

    Must run before any worker imports prometheus_client.  Files left by the
    previous run would be added to this run's counters, so they are removed;
    one still held by a process that is just exiting is skipped rather than
    failing the start.
    """
    directory = lab.data_dir / f"prometheus-{svc.name}"
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.db"):
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one shopsim service")
    parser.add_argument("--service", required=True, choices=SERVICE_NAMES)
    parser.add_argument("--config", required=True, help="path to the lab config JSON (shopsim.json)")
    args = parser.parse_args()

    lab = load_config(args.config)
    svc = lab.service(args.service)
    limits = apply_limits(svc.name, svc.cpu_cores, svc.memory_mb)

    # Imported after the limits are in place.
    import uvicorn

    server_options = dict(
        host=lab.bind_host,
        port=svc.port,
        log_config=None,
        access_log=False,
        timeout_keep_alive=30,
        backlog=2048,
    )
    workers = _workers(svc)
    if workers == 1:
        uvicorn.run(_build_app(lab, svc, limits, worker=False), **server_options)
        return

    os.environ[_ENV_SERVICE] = svc.name
    os.environ[_ENV_CONFIG] = str(Path(args.config).resolve())
    os.environ[_ENV_LIMITS] = json.dumps(dataclasses.asdict(limits))
    os.environ["SHOPSIM_GIT_COMMIT"] = _git_commit()  # one git call instead of one per worker
    lab.data_dir.mkdir(parents=True, exist_ok=True)
    _prepare_multiprocess_metrics(lab, svc)
    uvicorn.run("shopsim.run:create_worker_app", factory=True, workers=workers, **server_options)


if __name__ == "__main__":
    main()
