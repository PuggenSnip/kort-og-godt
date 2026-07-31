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


def connect(url=None) -> Database:
    resolved = _to_url(url)
    connect_args = {"check_same_thread": False} \
        if resolved.startswith("sqlite") else {}
    engine = create_engine(resolved, connect_args=connect_args,
                           pool_pre_ping=True, future=True)
    metadata.create_all(engine)
    return Database(engine)
