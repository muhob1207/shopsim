"""orders — checkout orchestration.

``POST /orders``: reserve stock (inventory) -> authorise payment (payments)
-> store the order (orders-db) -> send the confirmation (notifications).
If the payment fails, the stock reservation is released again.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from shopsim.clients import DownstreamError
from shopsim.db import Database
from shopsim.logs import log
from shopsim.web import ServiceContext, ServiceModule

SEEDED_ORDERS = 5000
DB_POOL_SIZE = 8

_background: set[asyncio.Task] = set()


class OrderRequest(BaseModel):
    customerId: str
    productId: str
    quantity: int = Field(default=1, ge=1, le=10)
    paymentMethod: str = "card"


def _unit_price(product_id: str) -> float:
    try:
        n = int(product_id.split("-", 1)[1])
    except (IndexError, ValueError):
        n = 1
    return round(5 + (n % 97) * 1.5, 2)


def _schema_and_seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS orders ("
        " id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, product_id TEXT NOT NULL,"
        " quantity INTEGER NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL,"
        " payment_id TEXT, created_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, 'completed', NULL, ?)",
            [
                (f"o-{i}", f"c-{i % 2000 + 1}", f"p-{i % 500 + 1}", 1, _unit_price(f"p-{i % 500 + 1}"), now)
                for i in range(1, SEEDED_ORDERS + 1)
            ],
        )


def create(ctx: ServiceContext) -> ServiceModule:
    router = APIRouter()
    inventory = ctx.peer("inventory")
    payments = ctx.peer("payments")
    notifications = ctx.peer("notifications")

    async def startup(c: ServiceContext) -> None:
        db = Database(
            name="orders-db",
            path=c.data_file("orders.db"),
            pool_size=DB_POOL_SIZE,
            latency_ms=float(c.cfg.get("db_latency_ms", 5)),
            logger=c.logger,
        )
        await db.start(_schema_and_seed)
        c.state["db"] = db

    async def shutdown(c: ServiceContext) -> None:
        for task in list(_background):
            task.cancel()
        await c.state["db"].close()

    async def _notify(order_id: str, customer_id: str, channel: str) -> None:
        try:
            await notifications.call(
                "POST", "/notifications",
                json={"orderId": order_id, "customerId": customer_id, "channel": channel},
            )
        except DownstreamError as exc:
            log(ctx.logger, logging.WARNING, f"confirmation for order {order_id} not sent: {exc.detail}",
                event="notification_failed", order_id=order_id, channel=channel)

    @router.post("/orders", status_code=201)
    async def create_order(body: OrderRequest):
        order_id = f"o-{uuid.uuid4().hex[:12]}"
        amount = round(_unit_price(body.productId) * body.quantity, 2)

        reservation = await inventory.call(
            "POST", "/stock/reservations",
            json={"orderId": order_id, "sku": body.productId, "quantity": body.quantity},
        )
        if reservation.status_code != 201:
            return JSONResponse(reservation.json(), status_code=reservation.status_code)
        reservation_id = reservation.json()["reservationId"]

        try:
            payment = await payments.call(
                "POST", "/payments",
                json={"orderId": order_id, "customerId": body.customerId, "amount": amount, "method": body.paymentMethod},
            )
        except DownstreamError:
            await _release(reservation_id, order_id, reason="payment error")
            raise
        if payment.status_code != 201:
            await _release(reservation_id, order_id, reason="payment declined")
            return JSONResponse({"orderId": order_id, "status": "payment_declined"}, status_code=402)
        payment_id = payment.json()["paymentId"]

        db: Database = ctx.state["db"]
        async with db.connection() as s:
            await s.execute(
                "INSERT order",
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?)",
                (order_id, body.customerId, body.productId, body.quantity, amount, payment_id,
                 datetime.now(timezone.utc).isoformat()),
            )

        # Confirmation is sent asynchronously: checkout latency must not depend
        # on the e-mail provider.
        task = asyncio.create_task(_notify(order_id, body.customerId, "email"))
        _background.add(task)
        task.add_done_callback(_background.discard)

        return {"orderId": order_id, "status": "confirmed", "amount": amount, "paymentId": payment_id}

    async def _release(reservation_id: int, order_id: str, *, reason: str) -> None:
        log(ctx.logger, logging.WARNING, f"releasing stock reservation {reservation_id} of order {order_id}: {reason}",
            event="reservation_released", order_id=order_id, reservation_id=reservation_id, reason=reason)
        try:
            await inventory.call(
                "POST", "/stock/reservations/{reservationId}/release",
                path=f"/stock/reservations/{reservation_id}/release",
            )
        except DownstreamError as exc:
            log(ctx.logger, logging.ERROR, f"could not release reservation {reservation_id}: {exc.detail}",
                event="reservation_release_failed", order_id=order_id, reservation_id=reservation_id)

    @router.get("/orders/{orderId}")
    async def get_order(orderId: str):  # noqa: N803
        db: Database = ctx.state["db"]
        async with db.connection() as s:
            row = await s.fetch_one(
                "SELECT order",
                "SELECT id, customer_id, product_id, quantity, amount, status, payment_id, created_at"
                " FROM orders WHERE id = ?",
                (orderId,),
            )
        if row is None:
            raise HTTPException(status_code=404, detail=f"order {orderId} not found")
        return dict(row)

    return ServiceModule(router=router, startup=startup, shutdown=shutdown, description="Order management and checkout.")
