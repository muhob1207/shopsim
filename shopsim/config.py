"""Lab configuration (one JSON file shared by every shopsim service).

The file lives outside the repository (it holds host paths and limits, which
are environment, not code); ``run.py`` receives its path on the command line.
Service *code* versions come from ``versions.json`` in the repository root, so
a deployment is always a commit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SERVICE_NAMES = (
    "gateway",
    "products",
    "orders",
    "inventory",
    "payments",
    "notifications",
    "bank-sim",
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    port: int
    cpu_cores: float | None
    memory_mb: int | None
    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)


@dataclass(frozen=True)
class LabConfig:
    environment: str
    bind_host: str
    log_dir: Path
    data_dir: Path
    otlp_traces_endpoint: str | None
    access_log_sample_rate: float
    slow_request_ms: float
    downstream_timeout_sec: float
    services: dict[str, ServiceConfig]

    def service(self, name: str) -> ServiceConfig:
        try:
            return self.services[name]
        except KeyError:
            raise SystemExit(f"service '{name}' is not defined in the lab config") from None

    def peer_url(self, name: str) -> str:
        # Every shopsim service runs on the same lab host, so peers are always
        # reached over loopback; only Perftelion talks to the advertised address.
        return f"http://127.0.0.1:{self.service(name).port}"


def load_config(path: str | Path) -> LabConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    services: dict[str, ServiceConfig] = {}
    for name, spec in (raw.get("services") or {}).items():
        spec = dict(spec)
        port = int(spec.pop("port"))
        cpu = spec.pop("cpu_cores", None)
        mem = spec.pop("memory_mb", None)
        services[name] = ServiceConfig(
            name=name,
            port=port,
            cpu_cores=float(cpu) if cpu else None,
            memory_mb=int(mem) if mem else None,
            extra=spec,
        )
    return LabConfig(
        environment=str(raw.get("environment", "test")),
        bind_host=str(raw.get("bind_host", "0.0.0.0")),
        log_dir=Path(raw.get("log_dir", "logs")),
        data_dir=Path(raw.get("data_dir", "data")),
        otlp_traces_endpoint=raw.get("otlp_traces_endpoint") or None,
        access_log_sample_rate=float(raw.get("access_log_sample_rate", 0.1)),
        slow_request_ms=float(raw.get("slow_request_ms", 500)),
        downstream_timeout_sec=float(raw.get("downstream_timeout_sec", 3.0)),
        services=services,
    )


def load_versions() -> dict[str, str]:
    path = REPO_ROOT / "versions.json"
    if not path.exists():
        return {}
    return {str(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
