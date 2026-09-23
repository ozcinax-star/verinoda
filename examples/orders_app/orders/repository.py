"""Persistence layer: the only module that talks to SQLite."""

import sqlite3

from orders.config import DATABASE_URL


class OrderRepository:
    def __init__(self, url: str = DATABASE_URL):
        self.conn = sqlite3.connect(url)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL)"
        )

    def save(self, customer: str, total: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO orders (customer, total) VALUES (?, ?)", (customer, total)
        )
        self.conn.commit()
        return cur.lastrowid

    def get(self, order_id: int):
        row = self.conn.execute(
            "SELECT id, customer, total FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        return None if row is None else {"id": row[0], "customer": row[1], "total": row[2]}
