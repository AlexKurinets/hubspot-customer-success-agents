"""The code behind every action. Each handler is the only way its action reaches HubSpot.

Order per action: claim the ledger row (committed), make the HubSpot write with the action key stamped
on the record, log a note on the company saying what was done and why, mark the row done. Records are
created with their associations in the same call, so a failure can't leave an orphan task or email.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import psycopg

from . import deadletters, ledger
from .actions import PlannedAction
from .hubspot.client import (
    DEAL_TO_COMPANY,
    EMAIL_TO_COMPANY,
    EMAIL_TO_CONTACT,
    NOTE_TO_COMPANY,
    NOTE_TO_DEAL,
    TASK_TO_COMPANY,
    TASK_TO_CONTACT,
    HubSpotClient,
    HubSpotError,
    HubSpotUnavailable,
    assoc,
)
from .hubspot.schema import PREFIX
from .sources import mail

LABELS = {
    "create_renewal_deal": "Opened renewal deal",
    "update_renewal_risk": "Updated renewal risk",
    "open_recovery_task": "Opened recovery task",
    "create_expansion_task": "Opened expansion task",
    "escalate_to_csm": "Escalated to CSM",
    "send_checkin": "Sent check-in",
    "send_onboarding_reminder": "Sent onboarding reminder",
    "send_ticket_followup": "Sent ticket follow-up",
    "prepare_qbr": "QBR pack approved",
    "change_deal_amount": "Changed renewal amount",
}


class WriteNotAllowed(Exception):
    pass


def guard(properties: dict, human_fields: frozenset[str] = frozenset()) -> dict:
    """Agents write freely only to cs_agent_ properties. Anything else must be named by an approved action."""
    bad = sorted(k for k in properties if not k.startswith(PREFIX) and k not in human_fields)
    if bad:
        raise WriteNotAllowed(f"agents may not write {', '.join(bad)} without an approved action")
    return properties


def stamp(day: date) -> str:
    return f"{day.isoformat()}T14:00:00.000Z"


class Executor:
    def __init__(self, conn: psycopg.Connection, crm: HubSpotClient, today: date, source: str):
        self.conn, self.crm, self.today, self.source = conn, crm, today, source

    def run(self, action: PlannedAction, autonomy: str, approval_id: int | None = None,
            approved_by: str | None = None) -> str:
        """Returns done, duplicate, failed or dead."""
        if not ledger.claim(self.conn, action, autonomy, self.today, self.source, approval_id):
            return "duplicate"
        return self.perform(action, approved_by)

    def perform(self, action: PlannedAction, approved_by: str | None = None) -> str:
        try:
            object_type, object_id = HANDLERS[action.action_type](self, action, approved_by)
            if action.action_type != "prepare_qbr":
                self.note(action, approved_by, object_type, object_id)
        except HubSpotUnavailable as e:
            ledger.fail(self.conn, action.key, str(e))
            deadletters.add(self.conn, "action", action.key, action.to_dict(), str(e), self.today)
            return "dead"
        except (HubSpotError, WriteNotAllowed) as e:
            ledger.fail(self.conn, action.key, str(e))
            return "failed"
        ledger.complete(self.conn, action.key, object_type, object_id)
        return "done"

    # ---------------------------------------------------------------- shared pieces

    def note(self, action: PlannedAction, approved_by: str | None, object_type: str, object_id: int) -> int:
        who = f"approved by {approved_by}" if approved_by else "no approval needed"
        body = (f"[CS agent] {LABELS[action.action_type]}. {action.payload.get('why', '')}\n"
                f"Agent: {action.agent}, {who}. Action key: {action.key}")
        assocs = [assoc(action.company_id, NOTE_TO_COMPANY)]
        if object_type == "deals":
            assocs.append(assoc(object_id, NOTE_TO_DEAL))
        r = self.crm.create("notes", {"hs_note_body": body, "hs_timestamp": stamp(self.today),
                                      "hubspot_owner_id": str(action.owner_id or ""), "cs_agent_action_key": action.key},
                            assocs)
        return int(r["id"])

    def task(self, action: PlannedAction, priority: str = "MEDIUM") -> tuple[str, int]:
        p = action.payload
        due = self.today + timedelta(days=int(p.get("due_in_days", 3)))
        assocs = [assoc(action.company_id, TASK_TO_COMPANY)]
        if p.get("contact_id"):
            assocs.append(assoc(int(p["contact_id"]), TASK_TO_CONTACT))
        r = self.crm.create("tasks", {
            "hs_task_subject": p["subject"], "hs_task_body": p["body"], "hs_timestamp": stamp(due),
            "hs_task_status": "NOT_STARTED", "hs_task_priority": p.get("priority", priority), "hs_task_type": "TODO",
            "hubspot_owner_id": str(p.get("assignee_id") or action.owner_id or ""), "cs_agent_action_key": action.key,
        }, assocs)
        return "tasks", int(r["id"])


# ---------------------------------------------------------------- handlers


def _renewal_deal(ex: Executor, a: PlannedAction, _by) -> tuple[str, int]:
    p = a.payload
    r = ex.crm.create("deals", {
        "dealname": f"{a.company_name} renewal {p['renewal_date']}", "pipeline": "renewals", "dealstage": "renewal_open",
        "amount": str(p["amount"]), "closedate": p["renewal_date"], "hubspot_owner_id": str(a.owner_id or ""),
        "cs_agent_risk_tier": p["risk_tier"], "cs_agent_action_key": a.key,
    }, [assoc(a.company_id, DEAL_TO_COMPANY)])
    return "deals", int(r["id"])


def _renewal_risk(ex: Executor, a: PlannedAction, _by) -> tuple[str, int]:
    deal_id = int(a.payload["deal_id"])
    ex.crm.update("deals", deal_id, guard({"cs_agent_risk_tier": a.payload["risk_tier"]}))
    return "deals", deal_id


def _task(ex: Executor, a: PlannedAction, _by) -> tuple[str, int]:
    return ex.task(a, "HIGH" if a.action_type == "escalate_to_csm" else "MEDIUM")


def _email(ex: Executor, a: PlannedAction, _by) -> tuple[str, int]:
    p = a.payload
    mail.send(ex.conn, a.key, a.company_id, a.owner_id, p["to_email"], p["subject"], p["body"], ex.today)
    r = ex.crm.create("emails", {
        "hs_timestamp": stamp(ex.today), "hs_email_direction": "EMAIL", "hs_email_status": "SENT",
        "hs_email_subject": p["subject"], "hs_email_text": p["body"], "hubspot_owner_id": str(a.owner_id or ""),
        "cs_agent_action_key": a.key,
    }, [assoc(int(p["contact_id"]), EMAIL_TO_CONTACT), assoc(a.company_id, EMAIL_TO_COMPANY)])
    return "emails", int(r["id"])


def _qbr(ex: Executor, a: PlannedAction, by) -> tuple[str, int]:
    p = a.payload
    body = (f"[CS agent] QBR pack for {p['meeting_date']} reviewed and approved by {by or 'CSM'}. "
            f"Figure check: {p['check_status']}. Draft {p['draft_id']}.\nAction key: {a.key}")
    r = ex.crm.create("notes", {"hs_note_body": body, "hs_timestamp": stamp(ex.today),
                                "hubspot_owner_id": str(a.owner_id or ""), "cs_agent_action_key": a.key},
                      [assoc(a.company_id, NOTE_TO_COMPANY)])
    return "notes", int(r["id"])


def _deal_amount(ex: Executor, a: PlannedAction, by) -> tuple[str, int]:
    if not by:
        raise WriteNotAllowed("amount is a human-owned field; it needs an approved action")
    deal_id = int(a.payload["deal_id"])
    ex.crm.update("deals", deal_id, guard({"amount": str(a.payload["new_amount"])}, frozenset({"amount"})))
    return "deals", deal_id


HANDLERS: dict[str, Callable[[Executor, PlannedAction, str | None], tuple[str, int]]] = {
    "create_renewal_deal": _renewal_deal,
    "update_renewal_risk": _renewal_risk,
    "open_recovery_task": _task,
    "create_expansion_task": _task,
    "escalate_to_csm": _task,
    "send_checkin": _email,
    "send_onboarding_reminder": _email,
    "send_ticket_followup": _email,
    "prepare_qbr": _qbr,
    "change_deal_amount": _deal_amount,
}
