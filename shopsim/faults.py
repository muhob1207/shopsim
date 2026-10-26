"""Runtime fault injection (``PUT /admin/faults``).

Code regressions are the primary way to create problems — they are real
commits the AI can read.  These knobs exist for quick, ad-hoc tests where a
commit would be overkill (for example: "make Loki show some errors so the
log querier has something to return").  They reset on restart.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field


class InjectedFault(Exception):
    """Raised for the requests selected by ``error_rate``."""


@dataclass
class Faults:
    latency_ms: float = 0.0
    error_rate: float = 0.0
    cpu_burn_ms: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


class FaultsPatch(BaseModel):
    latency_ms: float | None = Field(default=None, ge=0, le=60_000)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    cpu_burn_ms: float | None = Field(default=None, ge=0, le=10_000)


FAULTS = Faults()


def burn_cpu(ms: float) -> None:
    """Busy-loop on the calling thread for *ms* milliseconds of wall time."""
    deadline = time.perf_counter() + ms / 1000.0
    x = 0
    while time.perf_counter() < deadline:
        x += 1


async def apply() -> None:
    if FAULTS.cpu_burn_ms:
        burn_cpu(FAULTS.cpu_burn_ms)  # deliberately on the event loop, like real CPU-bound code
    if FAULTS.latency_ms:
        await asyncio.sleep(FAULTS.latency_ms / 1000.0)
    if FAULTS.error_rate and random.random() < FAULTS.error_rate:
        raise InjectedFault("injected fault (PUT /admin/faults error_rate)")
