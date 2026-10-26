"""notifications — sends order confirmations (simulated e-mail / SMS provider)."""
from __future__ import annotations

import asyncio
import logging
import random

from fastapi import APIRouter
from pydantic import BaseModel

from shopsim.logs import log
from shopsim.web import ServiceContext, ServiceModule

PROVIDER_LATENCY_MS = {"email": 120.0, "sms": 80.0, "push": 40.0}


class NotificationRequest(BaseModel):
    orderId: str
    customerId: str
    channel: str = "email"


def create(ctx: ServiceContext) -> ServiceModule:
    router = APIRouter()

    @router.post("/notifications", status_code=202)
    async def send(body: NotificationRequest):
        base = PROVIDER_LATENCY_MS.get(body.channel, 100.0)
        await asyncio.sleep(random.uniform(0.7, 1.3) * base / 1000.0)
        log(ctx.logger, logging.INFO, f"{body.channel} confirmation sent for order {body.orderId}",
            event="notification_sent", order_id=body.orderId, customer_id=body.customerId, channel=body.channel)
        return {"orderId": body.orderId, "channel": body.channel, "status": "sent"}

    return ServiceModule(router=router, description="Order notifications.")
