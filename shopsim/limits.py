"""Per-service resource limits — the Windows counterpart of a container cgroup.

Each shopsim service assigns *itself* to a Windows Job Object at start-up:

* CPU: a hard cap (``JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP``).  Windows expresses
  the cap as a share of the whole machine, so ``cpu_cores: 0.5`` on an 8-way
  host becomes 6.25 % (625 / 10 000).  Like a CFS quota, a capped process is
  throttled — it does not crash — so CPU saturation shows up as latency.
* Memory: a per-process committed-memory limit
  (``JOB_OBJECT_LIMIT_PROCESS_MEMORY``).  Allocations past it fail and Python
  raises ``MemoryError`` — the equivalent of hitting a container memory limit.

Child processes inherit the job.  On non-Windows hosts the limits are only
reported, never enforced.  If the job cannot be applied (for example the
process already sits in a job that forbids nesting) the service keeps running
unlimited and says so in its logs, ``/health`` and the ``sut_limits_enforced``
metric — a silently unlimited service would invalidate a bottleneck test.
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass

import psutil

MB = 1024 * 1024


@dataclass(frozen=True)
class AppliedLimits:
    cpu_cores: float            # effective CPU cap in cores (logical CPU count when uncapped)
    memory_bytes: int | None    # committed-memory cap; None when uncapped
    enforced: bool
    mechanism: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "cpu_cores": self.cpu_cores,
            "memory_mb": round(self.memory_bytes / MB) if self.memory_bytes else None,
            "enforced": self.enforced,
            "mechanism": self.mechanism,
            "detail": self.detail,
        }


def apply_limits(name: str, cpu_cores: float | None, memory_mb: int | None) -> AppliedLimits:
    logical = os.cpu_count() or 1
    wanted_cpu = min(cpu_cores, float(logical)) if cpu_cores else float(logical)
    wanted_mem = int(memory_mb * MB) if memory_mb else None

    if not cpu_cores and not memory_mb:
        return AppliedLimits(wanted_cpu, None, False, "none", "no limits configured")
    if sys.platform != "win32":
        return AppliedLimits(
            wanted_cpu, wanted_mem, False, "none",
            "limits are enforced on Windows only; values are reported for reference",
        )
    try:
        _apply_job_object(name, cpu_cores, wanted_mem, logical)
    except OSError as exc:
        return AppliedLimits(wanted_cpu, wanted_mem, False, "job-object", f"not applied: {exc}")
    return AppliedLimits(wanted_cpu, wanted_mem, True, "job-object")


def total_memory_bytes() -> int:
    return int(psutil.virtual_memory().total)


# ---------------------------------------------------------------------------
# Windows Job Object plumbing
# ---------------------------------------------------------------------------

_JOB_HANDLE = None  # kept for the life of the process; closing it would end the job

_JobObjectExtendedLimitInformation = 9
_JobObjectCpuRateControlInformation = 15
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4


def _apply_job_object(name: str, cpu_cores: float | None, memory_bytes: int | None, logical: int) -> None:
    global _JOB_HANDLE
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
        # ControlFlags + a 4-byte union (CpuRate | Weight | MinRate/MaxRate).
        _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    def _check(ok: int, what: str) -> None:
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(err, f"{what} failed: {ctypes.FormatError(err).strip()}")

    job = kernel32.CreateJobObjectW(None, f"shopsim-{name}-{os.getpid()}")
    if not job:
        _check(0, "CreateJobObjectW")

    if memory_bytes:
        ext = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ext.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_PROCESS_MEMORY
        ext.ProcessMemoryLimit = memory_bytes
        _check(
            kernel32.SetInformationJobObject(
                job, _JobObjectExtendedLimitInformation, ctypes.byref(ext), ctypes.sizeof(ext),
            ),
            "SetInformationJobObject(memory)",
        )

    if cpu_cores and cpu_cores < logical:
        rate = max(1, min(10_000, int(round(cpu_cores / logical * 10_000))))
        cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        cpu.ControlFlags = _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        cpu.CpuRate = rate
        _check(
            kernel32.SetInformationJobObject(
                job, _JobObjectCpuRateControlInformation, ctypes.byref(cpu), ctypes.sizeof(cpu),
            ),
            "SetInformationJobObject(cpu)",
        )

    _check(kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()), "AssignProcessToJobObject")
    _JOB_HANDLE = job
