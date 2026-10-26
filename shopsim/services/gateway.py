"""gateway — public entry point; routes /api/* to the internal services.

This is the only service Perftelion's load generator calls.  Its OpenAPI spec
(``GET /openapi.json``) is the one the catalog import test uses.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shopsim.web import ServiceContext, ServiceModule


class OrderRequest(BaseModel):
    customerId: str = Field(examples=["c-42"])
    productId: str = Field(examples=["p-17"])
    quantity: int = Field(default=1, ge=1, le=10)
    paymentMethod: Literal["card", "paypal", "bank_transfer"] = "card"


def _relay(resp) -> JSONResponse:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text[:300]}
    return JSONResponse(body, status_code=resp.status_code)


def create(ctx: ServiceContext) -> ServiceModule:
    router = APIRouter(prefix="/api")
    products = ctx.peer("products")
    orders = ctx.peer("orders")

    @router.get("/products", summary="List products", tags=["catalog"])
    async def list_products(
        category: str | None = Query(default=None, description="books|electronics|garden|toys|kitchen|sports"),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        params = {"limit": limit}
        if category:
            params["category"] = category
        return _relay(await products.call("GET", "/products", params=params))

    @router.get("/products/{productId}", summary="Get product with availability", tags=["catalog"])
    async def get_product(productId: str):  # noqa: N803 — name is part of the public API
        return _relay(await products.call("GET", "/products/{productId}", path=f"/products/{productId}"))

    @router.post("/orders", status_code=201, summary="Place an order", tags=["orders"])
    async def create_order(body: OrderRequest):
        return _relay(await orders.call("POST", "/orders", json=body.model_dump()))

    @router.get("/orders/{orderId}", summary="Get an order", tags=["orders"])
    async def get_order(orderId: str):  # noqa: N803
        return _relay(await orders.call("GET", "/orders/{orderId}", path=f"/orders/{orderId}"))

    return ServiceModule(router=router, description="Public API gateway of the shopsim web shop.")
