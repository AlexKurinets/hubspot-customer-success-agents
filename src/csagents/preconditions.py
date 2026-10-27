"""Checks that run in code before an action is allowed anywhere near a customer.

The billing ones exist because an automated check-in once reached a clinic group that was in a payment
dispute, which lived only in the billing system. They read billing directly instead of trusting HubSpot.
Blocked actions are recorded so the weekly report shows CSMs what the agents chose not to do.
"""

from __future__ import annotations

from datetime import date

import psycopg

from .actions import PlannedAction
from .sources.billing import BillingState


def check(action: PlannedAction, billing: BillingState | None) -> tuple[str, str] | None:
    """Returns (precondition, detail) when the action must not run, otherwise None."""
    if action.customer_facing:
        if billing is not None and billing.open_dispute:
            return "open_dispute", "billing shows an open payment dispute"
        if billing is not None and billing.failed_payment_30d:
            return "failed_payment", "a payment failed in the last 30 days"
        if not action.payload.get("to_email"):
            return "no_contact", "no contact with an email address on the account"
    return None


def record_block(conn: psycopg.Connection, action: PlannedAction, today: date, precondition: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO ops.blocked_actions (business_date, action_type, company_id, action_key, precondition, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (action_key) DO NOTHING",
        (today, action.action_type, action.company_id, action.key, precondition, detail))
    conn.commit()
