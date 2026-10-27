"""Webhook-driven work. The receiver (api.py) stores each event once by its event id and queues a job;
the worker claims jobs one at a time. A renewal-date change re-evaluates that one account straight away,
which is exactly the case that used to race the nightly run; both now produce the same ledger key."""

from __future__ import annotations

from collections import Counter

import psycopg
from psycopg.types.json import Jsonb

from .. import deadletters
from ..hubspot.client import HubSpotUnavailable
from . import health
from .context import AgentContext
from .dispatch import dispatch

MAX_ATTEMPTS = 5


def enqueue(conn: psycopg.Connection, kind: str, payload: dict) -> None:
    conn.execute("INSERT INTO ops.jobs (kind, payload) VALUES (%s, %s)", (kind, Jsonb(payload)))


def _claim(conn: psycopg.Connection) -> dict | None:
    row = conn.execute(
        "UPDATE ops.jobs SET status = 'running', attempts = attempts + 1 WHERE id = (SELECT id FROM ops.jobs "
        "WHERE status = 'queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *").fetchone()
    conn.commit()
    return row


def handle(ctx: AgentContext, job: dict) -> Counter:
    e = job["payload"]
    if job["kind"] == "company.propertyChange:cs_renewal_date":
        actions, _ = health.plan(ctx, company_ids=[int(e["objectId"])], renewal_only=True)
        return dispatch(ctx, actions)
    # Deal amount and stage changes need no immediate work: an approved amount change re-reads the deal
    # and its history before it writes, which is where a person's edit cancels it.
    return Counter(noted=1)


def process(ctx: AgentContext, limit: int = 500) -> dict:
    stats: Counter = Counter()
    for _ in range(limit):
        job = _claim(ctx.conn)
        if job is None:
            break
        try:
            stats.update(handle(ctx, job))
        except HubSpotUnavailable as ex:
            ctx.conn.rollback()
            ctx.conn.execute("UPDATE ops.jobs SET status = 'dead', error = %s WHERE id = %s", (str(ex)[:500], job["id"]))
            deadletters.add(ctx.conn, "job", str(job["id"]), {"kind": job["kind"], **job["payload"]}, str(ex), ctx.today)
            stats["dead"] += 1
            continue
        except Exception as ex:  # noqa: BLE001 - a bad job must not stop the queue
            ctx.conn.rollback()
            status = "dead" if job["attempts"] >= MAX_ATTEMPTS else "queued"
            ctx.conn.execute("UPDATE ops.jobs SET status = %s, error = %s WHERE id = %s", (status, str(ex)[:500], job["id"]))
            ctx.conn.commit()
            stats["errors"] += 1
            continue
        ctx.conn.execute("UPDATE ops.jobs SET status = 'done', finished_at = now() WHERE id = %s", (job["id"],))
        ctx.conn.commit()
        stats["jobs"] += 1
    return dict(stats)
