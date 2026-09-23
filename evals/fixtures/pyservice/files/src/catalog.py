"""The product catalog, in sqlite."""

import sqlite3


def connect():
    """A fresh in-memory catalog."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE products ("
        " id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,"
        " price_cents INTEGER NOT NULL, stock INTEGER NOT NULL DEFAULT 0)"
    )
    return conn


def add_product(conn, name, category, price_cents, stock=0):
    cur = conn.execute(
        "INSERT INTO products (name, category, price_cents, stock) VALUES (?, ?, ?, ?)",
        (name, category, price_cents, stock),
    )
    return cur.lastrowid


def search(conn, term):
    """Products whose name contains `term`, cheapest first, as (name, price_cents)."""
    sql = f"SELECT name, price_cents FROM products WHERE name LIKE '%{term}%' ORDER BY price_cents"
    return conn.execute(sql).fetchall()


def in_category(conn, category):
    """Every product in `category`, by name."""
    return conn.execute(
        "SELECT name FROM products WHERE category = ? ORDER BY name", (category,)
    ).fetchall()
