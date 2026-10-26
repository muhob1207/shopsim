"""payments — authorises card payments with the (simulated) external bank."""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shopsim.clients import DownstreamError
from shopsim.logs import log
from shopsim.web import ServiceContext, ServiceModule

# The bank's p99 authorisation latency is roughly 650 ms; the timeout leaves
# room for its occasional slow tail rather than failing those checkouts.
BANK_TIMEOUT_SEC = 2.0


class PaymentRequest(BaseModel):
    orderId: str
    customerId: str
    amount: float = Field(gt=0)
    method: str = "card"


def create(ctx: ServiceContext) -> ServiceModule:
    router = APIRouter()
    bank = ctx.peer("bank-sim", timeout_sec=BANK_TIMEOUT_SEC)

    @router.post("/payments", status_code=201)
    async def authorize(body: PaymentRequest):
        payment_id = f"pay-{uuid.uuid4().hex[:12]}"
        try:
            resp = await bank.call(
                "POST", "/v1/authorizations",
                json={"paymentId": payment_id, "amount": body.amount, "currency": "EUR", "method": body.method},
            )
        except DownstreamError as exc:
            log(
                ctx.logger, logging.ERROR,
                f"bank authorization failed for order {body.orderId}: {exc.detail}",
                event="payment_failed", order_id=body.orderId, customer_id=body.customerId,
                payment_id=payment_id, peer_service="bank-sim", error_type="BankUnavailable",
            )
            raise
        result = resp.json()
        if not result.get("approved"):
            log(ctx.logger, logging.INFO, f"payment declined for order {body.orderId}",
                event="payment_declined", order_id=body.orderId, payment_id=payment_id)
            return JSONResponse({"paymentId": payment_id, "status": "declined"}, status_code=402)
        return {"paymentId": payment_id, "status": "authorized", "authCode": result.get("authCode")}

    return ServiceModule(router=router, description="Payment authorisation.")
