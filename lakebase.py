"""
Lakebase (Databricks-managed Postgres) connection helper.

Connects via a single LAKEBASE_URL stored in a Databricks secret scope.
Provides a context-managed connection, plus run_query / run_write / run_insert
helpers used by both the Flask app and the MCP server.
"""

import base64
import os
from contextlib import contextmanager

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor

_w = WorkspaceClient()

_SCOPE = os.environ.get("RESEARCH_SECRET_SCOPE", "research_copilot")
_KEY = os.environ.get("LAKEBASE_SECRET_URL", "lakebase-url")


def _lakebase_url() -> str:
    """Fetch and decode the Lakebase connection URL from the Databricks secret scope."""
    secret = _w.secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for Lakebase (lazy import avoids hard dependency)."""
    from sqlalchemy import create_engine
    return create_engine(_lakebase_url())


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT / UPDATE / DELETE against Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


def run_insert(sql: str, batch_rows: list[tuple]) -> int:
    """Insert multiple rows into Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, batch_rows)
            conn.commit()
            return cur.rowcount


def run_query_one(sql: str, params: tuple | dict | None = None) -> dict | None:
    """Run a read query and return a single row (or None)."""
    rows = run_query(sql, params)
    return rows[0] if rows else None


def run_returning(sql: str, params: tuple | dict | None = None) -> dict | None:
    """Run a write query with a RETURNING clause and return the first row."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            row = cur.fetchone()
            return dict(row) if row else None


# ---------------------------------------------------------------------------
# Schema DDL (can be called from notebooks or setup scripts)
# ---------------------------------------------------------------------------

def init_schema():
    """Create all application tables if they don't exist.

    Non-destructive: uses CREATE TABLE IF NOT EXISTS so existing data is
    preserved.  To rebuild from scratch, DROP the tables manually first.
    """
    import pathlib

    sql_path = pathlib.Path(__file__).with_name("setup_database.sql")
    ddl = sql_path.read_text()

    with get_connection() as conn:
        conn.set_session(autocommit=True)
        with conn.cursor() as cur:
            cur.execute(ddl)
    print("✅ Schema initialised successfully.")
