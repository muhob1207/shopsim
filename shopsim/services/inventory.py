"""inventory — stock levels and reservations, backed by inventory-db."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from shopsim.db import Database
from shopsim.web import ServiceContext, ServiceModule

PRODUCT_COUNT = 500
INITIAL_STOCK = 1_000_000

# Connections held open to inventory-db.  Sized for peak checkout + browse
# traffic with headroom; see docs/capacity.md in this repository.
DB_POOL_SIZE = 16


class ReservationRequest(BaseModel):
    orderId: str
    sku: str
    quantity: int = Field(default=1, ge=1, le=100)


def _schema_and_seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stock ("
        " sku TEXT PRIMARY KEY, available INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reservations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, sku TEXT NOT NULL,"
        " quantity INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    if conn.execute("SELECT COUNT(*) FROM stock").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO stock (sku, available, reserved) VALUES (?, ?, 0)",
            [(f"p-{i}", INITIAL_STOCK) for i in range(1, PRODUCT_COUNT + 1)],
        )


def create(ctx: ServiceContext) -> ServiceModule:
    router = APIRouter()

    async def startup(c: ServiceContext) -> None:
        db = Database(
            name="inventory-db",
            path=c.data_file("inventory.db"),
            pool_size=DB_POOL_SIZE,
            latency_ms=float(c.cfg.get("db_latency_ms", 15)),
            acquire_timeout_sec=float(c.cfg.get("db_acquire_timeout_sec", 2.0)),
            logger=c.logger,
        )
        await db.start(_schema_and_seed)
        c.state["db"] = db

    async def shutdown(c: ServiceContext) -> None:
        await c.state["db"].close()

    @router.get("/stock/{sku}")
    async def get_stock(sku: str):
        db: Database = ctx.state["db"]
        async with db.connection() as s:
            row = await s.fetch_one("SELECT stock", "SELECT sku, available, reserved FROM stock WHERE sku = ?", (sku,))
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown sku {sku}")
        return dict(row)

    @router.post("/stock/reservations", status_code=201)
    async def reserve(body: ReservationRequest):
        db: Database = ctx.state["db"]
        now = datetime.now(timezone.utc).isoformat()
        async with db.connection() as s:
            changed = await s.execute(
                "UPDATE stock (reserve)",
                "UPDATE stock SET available = available - ?, reserved = reserved + ? WHERE sku = ? AND available >= ?",
                (body.quantity, body.quantity, body.sku, body.quantity),
            )
            if changed == 0:
                raise HTTPException(status_code=409, detail=f"insufficient stock for {body.sku}")
            rows = await s.fetch_all(
                "INSERT reservation",
                "INSERT INTO reservations (order_id, sku, quantity, status, created_at)"
                " VALUES (?, ?, ?, 'held', ?) RETURNING id",
                (body.orderId, body.sku, body.quantity, now),
            )
        return {"reservationId": int(rows[0]["id"]), "sku": body.sku, "quantity": body.quantity}

    @router.post("/stock/reservations/{reservationId}/release")
    async def release(reservationId: int):  # noqa: N803
        db: Database = ctx.state["db"]
        async with db.connection() as s:
            row = await s.fetch_one(
                "SELECT reservation",
                "SELECT sku, quantity, status FROM reservations WHERE id = ?",
                (reservationId,),
            )
            if row is None:
                raise HTTPException(status_code=404, detail=f"reservation {reservationId} not found")
            if row["status"] == "held":
                await s.execute(
                    "UPDATE stock (release)",
                    "UPDATE stock SET available = available + ?, reserved = reserved - ? WHERE sku = ?",
                    (row["quantity"], row["quantity"], row["sku"]),
                )
                await s.execute(
                    "UPDATE reservation (release)",
                    "UPDATE reservations SET status = 'released' WHERE id = ?",
                    (reservationId,),
                )
        return {"reservationId": reservationId, "status": "released"}

    return ServiceModule(router=router, startup=startup, shutdown=shutdown, description="Stock and reservations.")
