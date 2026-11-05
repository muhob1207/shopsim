"""bank-sim — stand-in for the external card acquirer.

Latency is log-normal (median ~90 ms, p99 ~650 ms) to mimic a third-party API
with a long tail; 0.5 % of authorisations are declined.
"""
from __future__ import annotations

import asyncio
import math
import random
import uuid

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from shopsim.web import ServiceContext, ServiceModule


class AuthorizationRequest(BaseModel):
    paymentId: str
    amount: float
    currency: str = "USD"
    method: str = "card"
    customerReference: str | None = Field(
        default=None,
        description="Optional reference of the customer at the merchant. Informational only.",
    )


def create(ctx: ServiceContext) -> ServiceModule:
    router = APIRouter()
    median_ms = float(ctx.cfg.get("latency_median_ms", 90))
    sigma = float(ctx.cfg.get("latency_sigma", 0.85))
    decline_rate = float(ctx.cfg.get("decline_rate", 0.005))

    @router.post(
        "/v1/authorizations",
        summary="Authorize a card payment",
        description=(
            "Authorizes a payment with the simulated acquirer. Answers after a "
            "log-normally distributed delay; a small share of authorizations is declined."
        ),
    )
    async def authorize(
        body: AuthorizationRequest,
        channel: str | None = Query(
            default=None,
            description="Optional sales channel label (web, mobile, ...). Informational only.",
        ),
    ):
        delay_ms = min(5000.0, random.lognormvariate(math.log(median_ms), sigma))
        await asyncio.sleep(delay_ms / 1000.0)
        approved = random.random() >= decline_rate
        return {
            "paymentId": body.paymentId,
            "approved": approved,
            "authCode": uuid.uuid4().hex[:8].upper() if approved else None,
            "processingMs": round(delay_ms, 1),
        }

    @router.get(
        "/v1/acquirer-info",
        summary="Describe the acquirer",
        description="Static information about the simulated acquirer: its name and the currencies it accepts.",
    )
    async def acquirer_info():
        return {"acquirer": "bank-sim", "supportedCurrencies": ["EUR", "GBP", "USD"]}

    return ServiceModule(router=router, description="Simulated external bank / acquirer.")
