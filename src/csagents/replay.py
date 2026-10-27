"""Recovery procedures.

Dead letters: an action whose HubSpot call exhausted its retries is re-claimed and performed again; a
dead job goes back on the queue. Stale claims: a ledger row left at 'claimed' (the process died between
the HubSpot write and marking it done) is reconciled by searching HubSpot for the action key stamped on
the record, so a write that did land is marked done instead of being made twice. Failed runs: the
nightly LangGraph thread resumes from its last checkpoint.
"""

from __future__ import annotations

from . import deadletters, ledger
from .actions import PlannedAction
from .agents.context import AgentContext
from .agents.jobs import enqueue
from .executor import Executor

OBJECT_FOR = {"create_renewal_deal": "deals", "update_renewal_risk": "notes", "open_recovery_task": "tasks",
              "create_expansion_task": "tasks", "escalate_to_csm": "tasks", "send_checkin": "emails",
              "send_onboarding_reminder": "emails", "send_ticket_followup": "emails", "prepare_qbr": "notes",
              "change_deal_amount": "notes"}


def _approver(ctx: AgentContext, key: str) -> str | None:
    row = ctx.conn.execute("SELECT a.decided_by FROM ops.ledger l JOIN ops.approvals a ON a.id = l.approval_id "
                           "WHERE l.action_key = %s", (key,)).fetchone()
    return row["decided_by"] if row else None


def dead_letters(ctx: AgentContext) -> dict:
    out = {"actions_done": 0, "actions_failed": 0, "jobs_requeued": 0}
    for d in deadletters.pending(ctx.conn):
        if d["source"] == "action":
            a = PlannedAction.from_dict(d["payload"])
            if ledger.reclaim(ctx.conn, a.key, "replay"):
                status = Executor(ctx.conn, ctx.crm, ctx.today, "replay").perform(a, _approver(ctx, a.key))
                out["actions_done" if status == "done" else "actions_failed"] += 1
                if status != "done":
                    continue
        else:
            payload = dict(d["payload"])
            enqueue(ctx.conn, payload.pop("kind"), payload)
            ctx.conn.commit()
            out["jobs_requeued"] += 1
        deadletters.mark_replayed(ctx.conn, d["id"])
    return out


def stale_claims(ctx: AgentContext, older_than_minutes: int = 10) -> dict:
    out = {"found_in_hubspot": 0, "performed": 0}
    for row in ledger.stale_claims(ctx.conn, older_than_minutes):
        object_type = OBJECT_FOR[row["action_type"]]
        hits = ctx.crm.search(object_type, [{"propertyName": "cs_agent_action_key", "operator": "EQ",
                                             "value": row["action_key"]}], ["cs_agent_action_key"], limit=1)["results"]
        if hits:
            ledger.complete(ctx.conn, row["action_key"], object_type, int(hits[0]["id"]))
            out["found_in_hubspot"] += 1
            continue
        a = PlannedAction(row["action_type"], row["agent"], row["company_id"], row["company_name"], row["owner_id"],
                          row["trigger"], row["payload"])
        Executor(ctx.conn, ctx.crm, ctx.today, "replay").perform(a, _approver(ctx, a.key))
        out["performed"] += 1
    return out
