from __future__ import annotations

from datetime import date

import psycopg
from psycopg.types.json import Jsonb


def add(conn: psycopg.Connection, source: str, ref: str, payload: dict, error: str, today: date) -> None:
    conn.execute("INSERT INTO ops.dead_letters (source, ref, payload, error, business_date) VALUES (%s, %s, %s, %s, %s)",
                 (source, ref, Jsonb(payload), error[:500], today))
    conn.commit()


def pending(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("SELECT * FROM ops.dead_letters WHERE replayed_at IS NULL ORDER BY id").fetchall()


def mark_replayed(conn: psycopg.Connection, dead_letter_id: int) -> None:
    conn.execute("UPDATE ops.dead_letters SET replayed_at = now() WHERE id = %s", (dead_letter_id,))
    conn.commit()
