"""Start one shopsim service.

    python -m shopsim.run --service inventory --config C:\\perftelion-testing\\sut\\shopsim.json

Order matters: resource limits are applied first (so everything the service
allocates is counted against them), then logging, tracing and metrics, then
the web server.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
from datetime import datetime, timezone

from shopsim.config import REPO_ROOT, SERVICE_NAMES, load_config, load_versions
from shopsim.limits import apply_limits


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

    from shopsim import metrics, services
    from shopsim.logs import setup_logging
    from shopsim.telemetry import setup_tracing
    from shopsim.web import ServiceContext, create_app

    version = load_versions().get(svc.name, "0.0.0")
    commit = _git_commit()
    instance = f"{socket.gethostname()}:{svc.port}"
    logger = setup_logging(service=svc.name, version=version, env=lab.environment, instance=instance, log_dir=lab.log_dir)
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
    app = create_app(ctx, module)
    uvicorn.run(
        app,
        host=lab.bind_host,
        port=svc.port,
        log_config=None,
        access_log=False,
        timeout_keep_alive=30,
        backlog=2048,
    )


if __name__ == "__main__":
    main()
