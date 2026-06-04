"""B3 worker - τ-bench-style mock API for tool-call benchmarks.

A small in-memory shopping API. Each endpoint does a tiny bit of
JSON parsing + state mutation, simulating real tool calls.

Concurrency model:
  - Multi-process: uvicorn `--workers N` (default = container vCPUs)
    so we exploit all cores at the OS level.
  - Multi-thread within a process: FastAPI dispatches sync handlers
    on an anyio threadpool. Each thread holds its own SQLite
    connection (thread-local) against a shared in-memory database
    (`file::memory:?cache=shared`), so reads run truly in parallel
    and SQLite's native mutex handles the (rare) writes.

Why not a single global Python lock? It makes every endpoint
serialize, which caps utilisation at ~1 core regardless of how
many uvicorn workers we add. With thread-local connections we
typically hit 60-80% CPU on a 4xlarge under benchmark load.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from fastapi import Body, FastAPI, HTTPException

app = FastAPI()

# Shared in-memory database. The URI form + cache=shared lets
# multiple SQLite connections see the same database (each connection
# is otherwise its own private :memory: DB). check_same_thread=False
# is required because FastAPI dispatches handlers across a thread
# pool; SQLite's internal mutex still serialises writes safely.
DB_URI = "file::memory:?cache=shared"

_init_lock = threading.Lock()
_initialised = False
_tls = threading.local()


def get_conn() -> sqlite3.Connection:
    """One SQLite connection per worker thread.

    Anyio's threadpool is bounded (defaults to 40 threads), so we
    create at most that many connections per uvicorn worker process.
    """
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_URI, uri=True, check_same_thread=False)
        # journal_mode=MEMORY avoids the rollback journal disk path
        # (we're :memory: anyway, but this is the documented setting).
        conn.execute("PRAGMA journal_mode = MEMORY")
        conn.execute("PRAGMA synchronous = OFF")
        # Long busy timeout in case writes briefly contend.
        conn.execute("PRAGMA busy_timeout = 5000")
        _tls.conn = conn
    return conn


def _ensure_initialised() -> None:
    """Create schema + seed once per process. Subsequent calls are no-ops.

    Because the database is `cache=shared`, the first thread in the
    process to win this lock is enough; other threads in the same
    process see the same data through their own connections.
    """
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        c = get_conn()
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
        n = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if n == 0:
            c.executemany(
                "INSERT OR IGNORE INTO items VALUES (?,?,?,?)",
                [(f"SKU-{i}", f"item-{i}", 10.0 + i, 100) for i in range(200)],
            )
            c.commit()
        _initialised = True


@app.on_event("startup")
def _on_startup() -> None:
    # Seed up front so the first request doesn't pay the init cost.
    _ensure_initialised()


@app.get("/healthz")
def healthz() -> dict:
    _ensure_initialised()
    return {"ok": True}


@app.get("/search")
def search(q: str = "") -> dict:
    rows = get_conn().execute(
        "SELECT sku,name,price FROM items WHERE name LIKE ? LIMIT 10",
        (f"%{q}%",),
    ).fetchall()
    return {"results": [{"sku": r[0], "name": r[1], "price": r[2]} for r in rows]}


@app.get("/product/{sku_id}")
def product(sku_id: str) -> dict:
    row = get_conn().execute(
        "SELECT sku,name,price,qty FROM items WHERE sku=?",
        (f"SKU-{sku_id}",),
    ).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    return {"sku": row[0], "name": row[1], "price": row[2], "qty": row[3]}


@app.post("/cart/add")
def cart_add(payload: dict[str, Any] = Body(...)) -> dict:
    c = get_conn()
    c.execute(
        "INSERT INTO carts VALUES (?,?,?)",
        ("alice", payload.get("sku", "SKU-0"), payload.get("qty", 1)),
    )
    c.commit()
    return {"ok": True}


@app.get("/cart")
def cart() -> dict:
    rows = get_conn().execute(
        "SELECT sku,qty FROM carts WHERE user=?", ("alice",)
    ).fetchall()
    return {"items": [{"sku": r[0], "qty": r[1]} for r in rows]}


@app.post("/checkout")
def checkout(payload: dict[str, Any] = Body(...)) -> dict:
    c = get_conn()
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
    row = get_conn().execute(
        "SELECT id,total,status FROM orders WHERE user=? ORDER BY id DESC LIMIT 1",
        ("alice",),
    ).fetchone()
    return {"id": row[0] if row else None,
            "total": row[1] if row else 0,
            "status": row[2] if row else "none"}


@app.post("/order/refund")
def order_refund(payload: dict[str, Any] = Body(...)) -> dict:
    c = get_conn()
    c.execute(
        "UPDATE orders SET status='refunded' "
        "WHERE id=(SELECT MAX(id) FROM orders WHERE user=?)",
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
    rows = get_conn().execute(
        "SELECT sku,name,price FROM items ORDER BY RANDOM() LIMIT ?",
        (k,),
    ).fetchall()
    return {"items": [{"sku": r[0], "name": r[1], "price": r[2]} for r in rows]}


@app.get("/inventory")
def inventory(sku: str = "SKU-0") -> dict:
    row = get_conn().execute(
        "SELECT qty FROM items WHERE sku=?", (sku,)
    ).fetchone()
    return {"sku": sku, "qty": row[0] if row else 0}
