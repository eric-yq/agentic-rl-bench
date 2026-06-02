"""B3 worker - τ-bench-style mock API for tool-call benchmarks.

A small in-memory shopping API. Each endpoint does a tiny bit of
JSON parsing + state mutation, simulating real tool calls.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException

app = FastAPI()
_lock = threading.Lock()
_db = sqlite3.connect(":memory:", check_same_thread=False)


@contextmanager
def conn():
    with _lock:
        yield _db


def _init() -> None:
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                sku TEXT PRIMARY KEY, name TEXT, price REAL, qty INT
            );
            CREATE TABLE IF NOT EXISTS carts (
                user TEXT, sku TEXT, qty INT
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user TEXT, total REAL, status TEXT
            );
            """
        )
        for i in range(200):
            c.execute(
                "INSERT OR IGNORE INTO items VALUES (?,?,?,?)",
                (f"SKU-{i}", f"item-{i}", 10.0 + i, 100),
            )
        c.commit()


_init()


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/search")
def search(q: str = "") -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT sku,name,price FROM items WHERE name LIKE ? LIMIT 10",
            (f"%{q}%",),
        ).fetchall()
    return {"results": [{"sku": r[0], "name": r[1], "price": r[2]} for r in rows]}


@app.get("/product/{sku_id}")
def product(sku_id: str) -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT sku,name,price,qty FROM items WHERE sku=?",
            (f"SKU-{sku_id}",),
        ).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    return {"sku": row[0], "name": row[1], "price": row[2], "qty": row[3]}


@app.post("/cart/add")
def cart_add(payload: dict[str, Any] = Body(...)) -> dict:
    with conn() as c:
        c.execute(
            "INSERT INTO carts VALUES (?,?,?)",
            ("alice", payload.get("sku", "SKU-0"), payload.get("qty", 1)),
        )
        c.commit()
    return {"ok": True}


@app.get("/cart")
def cart() -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT sku,qty FROM carts WHERE user=?", ("alice",)
        ).fetchall()
    return {"items": [{"sku": r[0], "qty": r[1]} for r in rows]}


@app.post("/checkout")
def checkout(payload: dict[str, Any] = Body(...)) -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT c.sku, c.qty, i.price FROM carts c JOIN items i ON c.sku=i.sku WHERE c.user=?",
            ("alice",),
        ).fetchall()
        total = sum(r[1] * r[2] for r in rows)
        c.execute(
            "INSERT INTO orders (user, total, status) VALUES (?,?,?)",
            ("alice", total, "paid"),
        )
        oid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("DELETE FROM carts WHERE user=?", ("alice",))
        c.commit()
    return {"order_id": oid, "total": total}


@app.get("/order/last")
def order_last() -> dict:
    with conn() as c:
        row = c.execute(
            "SELECT id,total,status FROM orders WHERE user=? ORDER BY id DESC LIMIT 1",
            ("alice",),
        ).fetchone()
    return {"id": row[0] if row else None, "total": row[1] if row else 0, "status": row[2] if row else "none"}


@app.post("/order/refund")
def order_refund(payload: dict[str, Any] = Body(...)) -> dict:
    with conn() as c:
        c.execute(
            "UPDATE orders SET status='refunded' WHERE id=(SELECT MAX(id) FROM orders WHERE user=?)",
            ("alice",),
        )
        c.commit()
    return {"ok": True, "reason": payload.get("reason", "")}


@app.get("/profile")
def profile() -> dict:
    return {"user": "alice", "tier": "gold", "points": 1234}


@app.post("/profile/update")
def profile_update(payload: dict[str, Any] = Body(...)) -> dict:
    return {"ok": True, "name": payload.get("name", "")}


@app.get("/recommend")
def recommend(k: int = 5) -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT sku,name,price FROM items ORDER BY RANDOM() LIMIT ?",
            (k,),
        ).fetchall()
    return {"items": [{"sku": r[0], "name": r[1], "price": r[2]} for r in rows]}


@app.get("/inventory")
def inventory(sku: str = "SKU-0") -> dict:
    with conn() as c:
        row = c.execute("SELECT qty FROM items WHERE sku=?", (sku,)).fetchone()
    return {"sku": sku, "qty": row[0] if row else 0}
