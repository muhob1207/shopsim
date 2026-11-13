"""Prometheus metrics exposed on ``GET /metrics``.

Process metrics are produced with psutil because prometheus_client's built-in
process collector reads ``/proc`` and therefore exports nothing on Windows.
Two ratios make limit-relative saturation explicit — the signal an analyst
needs to call a service "the bottleneck":

* ``sut_cpu_utilization_of_limit_ratio``    CPU used / CPU cap (1.0 = throttled)
* ``sut_memory_utilization_of_limit_ratio`` committed memory / memory cap

Label names avoid ``uri``/``url``/``path`` on purpose: Perftelion's collectors
drop those as high-cardinality (``SERVER_METRICS_DENY_LABELS``).
"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("PROMETHEUS_DISABLE_CREATED_SERIES", "True")

import psutil  # noqa: E402
from prometheus_client import (  # noqa: E402
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily  # noqa: E402

from shopsim.limits import AppliedLimits, total_memory_bytes  # noqa: E402

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 0.75, 1.0, 2.0, 3.0, 5.0, 10.0)
WAIT_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)

HTTP_SERVER = Histogram(
    "http_server_requests_seconds", "Inbound HTTP request duration",
    ["method", "route", "status"], buckets=LATENCY_BUCKETS,
)
HTTP_IN_FLIGHT = Gauge("http_server_requests_in_flight", "Inbound HTTP requests currently being served")
HTTP_CLIENT = Histogram(
    "http_client_requests_seconds", "Outbound HTTP call duration to another shopsim service",
    ["peer", "method", "route", "status"], buckets=LATENCY_BUCKETS,
)
DB_POOL_SIZE = Gauge("sut_db_pool_size", "Configured database connection pool size")
DB_POOL_IN_USE = Gauge("sut_db_pool_in_use", "Database connections currently checked out")
DB_POOL_WAITING = Gauge("sut_db_pool_waiting", "Requests currently waiting for a database connection")
DB_POOL_WAIT = Histogram("sut_db_pool_wait_seconds", "Time spent waiting for a database connection", buckets=WAIT_BUCKETS)
DB_QUERY = Histogram("sut_db_query_seconds", "Database statement duration", ["operation"], buckets=LATENCY_BUCKETS)
APP_ERRORS = Counter("sut_app_errors", "Application errors by type", ["type"])
INFO = Gauge("sut_info", "Static service information (value is always 1)", ["service", "version", "env", "commit"])


class _ResourceSampler:
    """Samples process CPU every couple of seconds so a gauge can report cores in use."""

    def __init__(self, interval: float = 2.0) -> None:
        self._proc = psutil.Process()
        self._interval = interval
        self._lock = threading.Lock()
        self._cores_used = 0.0
        t = self._proc.cpu_times()
        self._last_cpu = t.user + t.system
        self._last_wall = time.monotonic()
        threading.Thread(target=self._run, name="resource-sampler", daemon=True).start()

    def _run(self) -> None:
        while True:
            time.sleep(self._interval)
            t = self._proc.cpu_times()
            cpu = t.user + t.system
            wall = time.monotonic()
            with self._lock:
                dt = wall - self._last_wall
                if dt > 0:
                    self._cores_used = max(0.0, (cpu - self._last_cpu) / dt)
                self._last_cpu, self._last_wall = cpu, wall

    @property
    def cores_used(self) -> float:
        with self._lock:
            return self._cores_used


class ProcessResourceCollector:
    def __init__(self, limits: AppliedLimits) -> None:
        self._limits = limits
        self._proc = psutil.Process()
        self._sampler = _ResourceSampler()
        self._mem_cap = limits.memory_bytes or total_memory_bytes()

    def collect(self):
        with self._proc.oneshot():
            cpu = self._proc.cpu_times()
            mem = self._proc.memory_info()
            threads = self._proc.num_threads()
        private = int(getattr(mem, "private", 0) or getattr(mem, "vms", 0))

        c = CounterMetricFamily("process_cpu_seconds", "Total user and system CPU time spent in seconds")
        c.add_metric([], cpu.user + cpu.system)
        yield c
        yield GaugeMetricFamily("process_resident_memory_bytes", "Resident (working set) memory", value=float(mem.rss))
        yield GaugeMetricFamily("process_private_memory_bytes", "Committed private memory (what the job memory limit counts)", value=float(private))
        yield GaugeMetricFamily("process_threads", "OS threads in the process", value=float(threads))
        yield GaugeMetricFamily("sut_cpu_limit_cores", "CPU cap applied to this service, in cores", value=float(self._limits.cpu_cores))
        yield GaugeMetricFamily("sut_memory_limit_bytes", "Memory cap applied to this service (host RAM when uncapped)", value=float(self._mem_cap))
        yield GaugeMetricFamily("sut_limits_enforced", "1 when the CPU/memory limits are enforced by the OS", value=1.0 if self._limits.enforced else 0.0)
        yield GaugeMetricFamily("sut_cpu_cores_used", "CPU cores used, averaged over the last ~2 s", value=round(self._sampler.cores_used, 4))
        yield GaugeMetricFamily(
            "sut_cpu_utilization_of_limit_ratio", "CPU used divided by the CPU cap (1.0 = throttled at the cap)",
            value=round(self._sampler.cores_used / max(self._limits.cpu_cores, 0.01), 4),
        )
        yield GaugeMetricFamily(
            "sut_memory_utilization_of_limit_ratio", "Committed memory divided by the memory cap",
            value=round(private / max(self._mem_cap, 1), 4),
        )


def install(limits: AppliedLimits, *, service: str, version: str, env: str, commit: str) -> None:
    try:  # Linux only: avoid a duplicate process_cpu_seconds_total.
        from prometheus_client import PROCESS_COLLECTOR

        REGISTRY.unregister(PROCESS_COLLECTOR)
    except Exception:  # noqa: BLE001
        pass
    REGISTRY.register(ProcessResourceCollector(limits))
    INFO.labels(service, version, env, commit).set(1)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
