import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATABASE = Path(os.environ.get("BOOSTER_DB", "data/boosters.sqlite3"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    path TEXT PRIMARY KEY, code TEXT NOT NULL, name TEXT NOT NULL,
    kind TEXT NOT NULL, catalog_ev TEXT, catalog_return TEXT,
    catalog_at TEXT NOT NULL, saved INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0, shop_amount TEXT,
    shop_currency TEXT, manual_fx TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE
);
CREATE TABLE IF NOT EXISTS memberships (
    folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    path TEXT REFERENCES products(path) ON DELETE CASCADE,
    PRIMARY KEY (folder_id, path)
);
CREATE TABLE IF NOT EXISTS snapshots (
    path TEXT REFERENCES products(path), captured_at TEXT NOT NULL,
    digest TEXT NOT NULL, metrics TEXT NOT NULL,
    UNIQUE (path, digest)
);
CREATE INDEX IF NOT EXISTS snapshots_latest ON snapshots(path, captured_at DESC);
CREATE TABLE IF NOT EXISTS fx (
    currency TEXT NOT NULL, rate_date TEXT NOT NULL, rate TEXT NOT NULL,
    PRIMARY KEY (currency, rate_date)
);
CREATE TABLE IF NOT EXISTS models (
    path TEXT PRIMARY KEY REFERENCES products(path),
    created_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@contextmanager
def connect():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            yield db
    finally:
        db.close()


def init():
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO settings VALUES ('csrf', lower(hex(randomblob(32))))")
        migration = db.execute("INSERT OR IGNORE INTO settings VALUES ('pinned_navigation', '1')")
        if migration.rowcount:
            db.execute("""
                UPDATE products SET pinned=1 WHERE saved=1 AND pinned=0 AND NOT EXISTS (
                    SELECT 1 FROM memberships WHERE memberships.path=products.path
                )
            """)


def save_catalog(products):
    timestamp = now()
    with connect() as db:
        db.executemany("""
            INSERT INTO products (path, code, name, kind, catalog_ev, catalog_return, catalog_at)
            VALUES (:path, :code, :name, :kind, :ev, :market_return, :catalog_at)
            ON CONFLICT(path) DO UPDATE SET code=excluded.code, name=excluded.name,
                kind=excluded.kind, catalog_ev=excluded.catalog_ev,
                catalog_return=excluded.catalog_return, catalog_at=excluded.catalog_at
        """, [{**product, "catalog_at": timestamp} for product in products])


def save_snapshot(path, metrics):
    payload = json.dumps(metrics, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with connect() as db:
        db.execute("""
            INSERT INTO snapshots VALUES (?, ?, ?, ?)
            ON CONFLICT(path, digest) DO UPDATE SET captured_at=excluded.captured_at
        """, (path, now(), digest, payload))
        db.execute("UPDATE products SET last_error=NULL WHERE path=?", (path,))
