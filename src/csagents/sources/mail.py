"""Outbound email from the CSM's own mailbox. Production sent through the Gmail API with domain-wide
delegation and stamped the action key in a header; here the outbox table is unique on the key, which
gives the same guarantee: a retried send finds the earlier one and does nothing."""

from __future__ import annotations

from datetime import date

import psycopg


def send(conn: psycopg.Connection, action_key: str, company_id: int, owner_id: int | None, to_email: str,
         subject: str, body: str, sent_on: date) -> bool:
    """Returns True if this call sent the message, False if it had already gone out."""
    row = conn.execute(
        "INSERT INTO ops.outbox (action_key, company_id, from_owner_id, to_email, subject, body, sent_on) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (action_key) DO NOTHING RETURNING id",
        (action_key, company_id, owner_id, to_email, subject, body, sent_on),
    ).fetchone()
    conn.commit()
    return row is not None
