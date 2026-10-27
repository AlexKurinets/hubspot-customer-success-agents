from __future__ import annotations

from datetime import date
from importlib import resources

import psycopg
from psycopg.rows import dict_row

from . import settings

SCHEMAS = ("ops", "wh", "billing", "sim")  # hs belongs to the HubSpot stand-in; it resets itself


def connect(url: str | None = None, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(url or settings.load().database_url, row_factory=dict_row, autocommit=autocommit)


def migrate(conn: psycopg.Connection) -> None:
    sql = resources.files("csagents").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.execute(sql)
    conn.commit()


def reset(conn: psycopg.Connection) -> None:
    """Drops the agents' schemas and LangGraph's checkpoints. The HubSpot stand-in resets through its own endpoint."""
    for s in SCHEMAS:
        conn.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")
    for t in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
        conn.execute(f"DROP TABLE IF EXISTS public.{t} CASCADE")
    conn.commit()
    migrate(conn)


def today(conn: psycopg.Connection) -> date:
    row = conn.execute("SELECT today FROM ops.clock WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("ops.clock is not set; run `csagents seed` first")
    return row["today"]


def set_today(conn: psycopg.Connection, day: date) -> None:
    conn.execute(
        "INSERT INTO ops.clock (id, today) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET today = EXCLUDED.today",
        (day,),
    )
    conn.commit()
