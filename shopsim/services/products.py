"""products — product catalogue; product detail also asks inventory for stock."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Query

from shopsim.db import Database
from shopsim.web import ServiceContext, ServiceModule

CATEGORIES = ("books", "electronics", "garden", "toys", "kitchen", "sports")
PRODUCT_COUNT = 500
DB_POOL_SIZE = 8


def _schema_and_seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS products ("
        " id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,"
        " price REAL NOT NULL, description TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS products_category ON products(category)")
    if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        rows = [
            (
                f"p-{i}",
                f"Product {i}",
                CATEGORIES[i % len(CATEGORIES)],
                round(5 + (i % 97) * 1.5, 2),
                f"Description of product {i}. " * 4,
            )
            for i in range(1, PRODUCT_COUNT + 1)
        ]
        conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", rows)


def create(ctx: ServiceContext) -> ServiceModule:
    router = APIRouter()
    inventory = ctx.peer("inventory")

    async def startup(c: ServiceContext) -> None:
        db = Database(
            name="products-db",
            path=c.data_file("products.db"),
            pool_size=DB_POOL_SIZE,
            latency_ms=float(c.cfg.get("db_latency_ms", 3)),
            logger=c.logger,
        )
        await db.start(_schema_and_seed)
        c.state["db"] = db

    async def shutdown(c: ServiceContext) -> None:
        await c.state["db"].close()

    @router.get("/products")
    async def list_products(category: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
        db: Database = ctx.state["db"]
        async with db.connection() as s:
            if category:
                rows = await s.fetch_all(
                    "SELECT products",
                    "SELECT id, name, category, price FROM products WHERE category = ? ORDER BY id LIMIT ?",
                    (category, limit),
                )
            else:
                rows = await s.fetch_all(
                    "SELECT products",
                    "SELECT id, name, category, price FROM products ORDER BY id LIMIT ?",
                    (limit,),
                )
        return {"items": [dict(r) for r in rows], "count": len(rows)}

    @router.get("/products/{productId}")
    async def get_product(productId: str):  # noqa: N803
        db: Database = ctx.state["db"]
        async with db.connection() as s:
            row = await s.fetch_one(
                "SELECT product by id",
                "SELECT id, name, category, price, description FROM products WHERE id = ?",
                (productId,),
            )
        if row is None:
            raise HTTPException(status_code=404, detail=f"product {productId} not found")
        stock = await inventory.call("GET", "/stock/{sku}", path=f"/stock/{productId}")
        product = dict(row)
        product["availability"] = stock.json() if stock.status_code == 200 else None
        return product

    return ServiceModule(router=router, startup=startup, shutdown=shutdown, description="Product catalogue.")
