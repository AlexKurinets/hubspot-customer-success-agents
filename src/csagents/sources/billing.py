"""Subscriptions, invoices and disputes. Production read these from Stripe; here from the billing schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import psycopg


@dataclass(frozen=True)
class BillingState:
    arr: float
    seats: int
    renewal_date: date
    invoice_current: bool
    open_dispute: bool
    failed_payment_30d: bool


def states(conn: psycopg.Connection, today: date, company_ids: list[int] | None = None) -> dict[int, BillingState]:
    where = " WHERE s.company_id = ANY(%s)" if company_ids is not None else ""
    rows = conn.execute(
        f"""SELECT s.company_id, s.arr, s.seats, s.current_period_end,
                   EXISTS (SELECT 1 FROM billing.invoices i WHERE i.company_id = s.company_id
                           AND i.status IN ('open', 'failed') AND i.due_on < %s) AS past_due,
                   EXISTS (SELECT 1 FROM billing.disputes d WHERE d.company_id = s.company_id
                           AND d.opened_on <= %s AND (d.closed_on IS NULL OR d.closed_on > %s)) AS dispute,
                   EXISTS (SELECT 1 FROM billing.invoices i WHERE i.company_id = s.company_id
                           AND i.status = 'failed' AND i.due_on BETWEEN %s AND %s) AS failed_30d
            FROM billing.subscriptions s{where}""",
        [today, today, today, today - timedelta(days=30), today] + ([company_ids] if company_ids is not None else []),
    ).fetchall()
    return {r["company_id"]: BillingState(float(r["arr"]), r["seats"], r["current_period_end"],
                                          not (r["past_due"] or r["dispute"]), r["dispute"], r["failed_30d"])
            for r in rows}
