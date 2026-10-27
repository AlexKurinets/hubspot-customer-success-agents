"""The action ledger: claim before writing, complete after.

`claim` inserts the action key under a unique constraint and commits before any HubSpot call. A second
claim for the same key, whether from a retried job, a redelivered webhook or a nightly run that reached
the same change, finds the row and stops. Claims that never completed (a crash between the HubSpot call
and `complete`) are reconciled by `replay --stale-claims`, which searches HubSpot for the key.
"""

from __future__ import annotations

from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from .actions import PlannedAction


def claim(conn: psycopg.Connection, action: PlannedAction, autonomy: str, today: date, source: str,
          approval_id: int | None = None) -> bool:
    row = conn.execute(
        """INSERT INTO ops.ledger (action_key, action_type, agent, company_id, company_name, owner_id, trigger,
                                   business_date, autonomy, approval_id, status, payload, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'claimed', %s, %s)
           ON CONFLICT (action_key) DO NOTHING RETURNING id""",
        (action.key, action.action_type, action.agent, action.company_id, action.company_name, action.owner_id,
         action.trigger, today, autonomy, approval_id, Jsonb(action.payload), source),
    ).fetchone()
    if row is None:
        conn.execute("INSERT INTO ops.ledger_collisions (action_key, business_date, source) VALUES (%s, %s, %s)",
                     (action.key, today, source))
    conn.commit()
    return row is not None


def reclaim(conn: psycopg.Connection, key: str, source: str) -> bool:
    """A failed action may be tried again (replay); a done one never is."""
    n = conn.execute("UPDATE ops.ledger SET status = 'claimed', error = NULL, source = %s, claimed_at = now() "
                     "WHERE action_key = %s AND status = 'failed'", (source, key)).rowcount
    conn.commit()
    return n == 1


def complete(conn: psycopg.Connection, key: str, hubspot_type: str, hubspot_id: int) -> None:
    conn.execute("UPDATE ops.ledger SET status = 'done', hubspot_type = %s, hubspot_id = %s, done_at = now() "
                 "WHERE action_key = %s", (hubspot_type, hubspot_id, key))
    conn.commit()


def fail(conn: psycopg.Connection, key: str, error: str) -> None:
    conn.execute("UPDATE ops.ledger SET status = 'failed', error = %s WHERE action_key = %s", (error[:500], key))
    conn.commit()


def done_before(conn: psycopg.Connection, keys: list[str], today: date) -> set[str]:
    """Keys finished on an earlier business date. Planning drops these quietly; anything claimed today is
    left to `claim`, so a same-day race between two paths shows up as a recorded collision."""
    if not keys:
        return set()
    rows = conn.execute("SELECT action_key FROM ops.ledger WHERE action_key = ANY(%s) AND business_date < %s",
                        (keys, today))
    return {r["action_key"] for r in rows}


def latest(conn: psycopg.Connection, action_type: str, company_id: int) -> dict | None:
    return conn.execute("SELECT * FROM ops.ledger WHERE action_type = %s AND company_id = %s AND status = 'done' "
                        "ORDER BY business_date DESC, id DESC LIMIT 1", (action_type, company_id)).fetchone()


def stale_claims(conn: psycopg.Connection, older_than_minutes: int = 10) -> list[dict]:
    return conn.execute("SELECT * FROM ops.ledger WHERE status = 'claimed' "
                        "AND claimed_at < now() - make_interval(mins => %s) ORDER BY id", (older_than_minutes,)).fetchall()
