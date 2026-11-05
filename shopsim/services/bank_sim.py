"""bank-sim — stand-in for the external card acquirer.

Latency is log-normal (median ~90 ms, p99 ~650 ms) to mimic a third-party API
with a long tail; 0.5 % of authorisations are declined.
"""
from __future__ import annotations

import asyncio
import math
import random
import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from shopsim.web import ServiceContext, ServiceModule


class AuthorizationRequest(BaseModel):
    paymentId: str
    amount: float
    currency: str = "EUR"
    method: str = "card"


def create(ctx: ServiceContext) -> ServiceModule:
    router = APIRouter()
    median_ms = float(ctx.cfg.get("latency_median_ms", 90))
    sigma = float(ctx.cfg.get("latency_sigma", 0.85))
    decline_rate = float(ctx.cfg.get("decline_rate", 0.005))

    @router.post("/v1/authorizations")
    async def authorize(body: AuthorizationRequest):
        delay_ms = min(5000.0, random.lognormvariate(math.log(median_ms), sigma))
        await asyncio.sleep(delay_ms / 1000.0)
        approved = random.random() >= decline_rate
        return {
            "paymentId": body.paymentId,
            "approved": approved,
            "authCode": uuid.uuid4().hex[:8].upper() if approved else None,
            "processingMs": round(delay_ms, 1),
        }

    return ServiceModule(router=router, description="Simulated external bank / acquirer.")
