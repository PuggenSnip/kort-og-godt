"""Database access for Kort og Godt.

One thin layer over SQLAlchemy so the exact same code runs on:
  * local SQLite    (default — a file next to the app, zero setup), and
  * shared Postgres (cloud — set DATABASE_URL, so 3 people see the same data).

Callers use a small sqlite3-like surface: ``conn.execute(sql, params).fetchall()``
with ``:named`` placeholders and dict params; rows come back as plain dicts.
Each execute() runs in its own autocommitted transaction, which keeps the
connection pool thread-safe under Streamlit's re-run model.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy import (Column, Float, Integer, MetaData, Table, Text,
                        create_engine, insert, text)
from sqlalchemy.engine import Engine

APP_DIR = Path(__file__).resolve().parent
DEFAULT_URL = f"sqlite:///{(APP_DIR / 'kortoggodt.db').as_posix()}"

metadata = MetaData()

scans = Table(
    "scans", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", Text, nullable=False),
    Column("finished_at", Text),
)
observations = Table(
    "observations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scan_id", Integer),
    Column("product_id", Text, nullable=False),
    Column("shop", Text, nullable=False),
    Column("url", Text),
    Column("method", Text),
    Column("title", Text),
    Column("observed_at", Text, nullable=False),
    Column("price_native", Float),
    Column("currency", Text),
    Column("price_dkk", Float),
    Column("landed_dkk", Float),
    Column("in_stock", Integer),
    Column("status", Text, nullable=False),
    Column("error", Text),
    Column("allocation_risk", Integer, default=0),
    Column("reference_only", Integer, default=0),
    Column("source_kind", Text),
    # Who entered this row (manual Cardmarket entries). NULL for scraped rows.
    # Retrofitted onto existing DBs by _ensure_columns() — see connect().
    Column("added_by", Text),
)
http_cache = Table(
    "http_cache", metadata,
    Column("url", Text, primary_key=True),
    Column("fetched_at", Text, nullable=False),
    Column("status", Integer),
    Column("body", Text),
)
collection_snapshots = Table(
    "collection_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("taken_at", Text, nullable=False),
    Column("basis", Text),
    Column("total_value_dkk", Float),
    Column("total_cost_dkk", Float),
    Column("total_invested_dkk", Float),
    Column("n_items", Integer),
    Column("n_valued", Integer),
    Column("n_unverified", Integer),
)
app_config = Table(
    "app_config", metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text),
)
feedback = Table(
    "feedback", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", Text, nullable=False),
    Column("person", Text),
    Column("message", Text, nullable=False),
    Column("app_version", Text),
    Column("product_id", Text),
    Column("resolved", Integer, default=0),
)


def _to_url(url) -> str:
    """Accept None (env/default), a DB URL, or a filesystem path to a SQLite db."""
    if url is None:
        url = os.environ.get("DATABASE_URL") or DEFAULT_URL
    s = str(url)
    if s.startswith(("sqlite:", "postgres:", "postgresql:")):
        # Supabase/Heroku hand out 'postgres://'; SQLAlchemy wants 'postgresql://'.
        if s.startswith("postgres://"):
            s = "postgresql://" + s[len("postgres://"):]
        return s
    return f"sqlite:///{Path(s).as_posix()}"


class Result:
    """Minimal cursor-like holder of already-fetched dict rows."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchone(self) -> Optional[dict]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict]:
        return self._rows


class Database:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.is_postgres = engine.dialect.name == "postgresql"

    def execute(self, sql: str, params: Optional[dict] = None) -> Result:
        stmt = text(sql) if isinstance(sql, str) else sql
        with self.engine.begin() as conn:
            res = conn.execute(stmt, params or {})
            rows = ([dict(m) for m in res.mappings().all()]
                    if res.returns_rows else [])
        return Result(rows)

    def insert_scan(self, started_at: str) -> int:
        """Insert a scan row and return its new id (portable lastrowid)."""
        with self.engine.begin() as conn:
            res = conn.execute(insert(scans).values(started_at=started_at))
            return int(res.inserted_primary_key[0])

    def commit(self) -> None:      # each execute() already autocommits
        pass

    def close(self) -> None:
        self.engine.dispose()


def _ensure_columns(engine: Engine) -> None:
    """Retrofit columns that create_all() can't add to an existing table.

    ``metadata.create_all`` only issues CREATE TABLE for missing tables; it
    never diffs columns or ALTERs an existing one. The shared Postgres DB (and
    any SQLite file) created before v0.2 has an ``observations`` table without
    ``added_by``, so add it idempotently on both dialects. Safe to run every
    startup — a fresh DB already has the column and this becomes a no-op.
    """
    is_postgres = engine.dialect.name == "postgresql"
    with engine.begin() as conn:
        if is_postgres:
            conn.execute(text(
                "ALTER TABLE observations "
                "ADD COLUMN IF NOT EXISTS added_by TEXT"))
        else:
            # SQLite has no ADD COLUMN IF NOT EXISTS — check first.
            existing = {row[1] for row in conn.execute(
                text("PRAGMA table_info(observations)")).fetchall()}
            if "added_by" not in existing:
                conn.execute(text(
                    "ALTER TABLE observations ADD COLUMN added_by TEXT"))


def connect(url=None) -> Database:
    resolved = _to_url(url)
    is_sqlite = resolved.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    # pool_pre_ping issues a liveness SELECT before EVERY checkout — a whole
    # extra round-trip per query, which doubles the cost against a remote
    # Postgres. Recycle connections older than the pooler's idle window instead:
    # a stale connection is discarded and remade on next use, without a ping on
    # every healthy query. (No effect on the local SQLite file pool.)
    engine = create_engine(
        resolved, connect_args=connect_args, future=True,
        pool_pre_ping=False,
        pool_recycle=(-1 if is_sqlite else 280))
    metadata.create_all(engine)
    _ensure_columns(engine)
    return Database(engine)
