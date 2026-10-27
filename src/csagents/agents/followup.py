"""Follow-up agent.

Triggers are decided in code: weekly active users down by a third against the prior four weeks, no
contact for 45 days, onboarding milestones 7, 30 and 60 days after go-live while a core module is still
off, and a high-priority ticket closed yesterday. For each triggered account the model reads the recent
text, flags cancellation or billing language, and proposes either a check-in from the CSM's mailbox or
an escalation. Cancellation language always becomes an escalation with no draft.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from ..actions import PlannedAction
from ..llm.base import LLMError
from ..sources import warehouse
from ..tools import ValidationError, validate
from .context import AgentContext
from .text import contacts_for, recent_text

ACTION_FOR_KIND = {"checkin": "send_checkin", "onboarding": "send_onboarding_reminder",
                   "ticket_followup": "send_ticket_followup"}
KIND_FOR_TRIGGER = {"usage_drop": "checkin", "no_contact": "checkin", "onboarding": "onboarding",
                    "ticket_followup": "ticket_followup"}
PRIORITY = ["ticket_followup", "onboarding", "usage_drop", "no_contact"]


def triggers(ctx: AgentContext) -> dict[int, dict]:
    cfg, today, conn = ctx.cfg, ctx.today, ctx.conn
    ids = warehouse.company_ids(conn)
    companies = ctx.companies(ids)
    usage = warehouse.recent_usage(conn, today, 5)
    found: dict[int, list[dict]] = {}
    for cid in ids:
        c = companies.get(cid)
        if c is None:
            continue
        weeks = usage.get(cid, [])
        if len(weeks) == 5:
            prior = sum(w.wau for w in weeks[:4]) / 4
            if prior >= 3 and weeks[4].wau < (1 - cfg.usage_drop_fraction) * prior:
                found.setdefault(cid, []).append({"kind": "usage_drop", "key": f"usage_drop:{weeks[4].week_start}",
                                                  "detail": f"weekly active users {weeks[4].wau} against {prior:.0f}"})
        if c["last_contacted"] and (today - c["last_contacted"]).days >= cfg.no_contact_days:
            found.setdefault(cid, []).append({"kind": "no_contact", "key": f"no_contact:{c['last_contacted']}",
                                              "detail": f"last contact {c['last_contacted']}"})
        if c["go_live"]:
            days = (today - c["go_live"]).days
            modules = weeks[-1].modules if weeks else 0
            if days in cfg.onboarding_days and modules < 3:
                found.setdefault(cid, []).append({"kind": "onboarding", "key": f"onboarding:{days}", "days": days,
                                                  "detail": f"day {days} after go-live, {modules} of 3 modules"})
    yesterday = today - timedelta(days=1)
    closed = list(ctx.crm.search_all("tickets", [
        {"propertyName": "hs_pipeline_stage", "operator": "EQ", "value": "4"},
        {"propertyName": "hs_ticket_priority", "operator": "EQ", "value": cfg.ticket_followup_priority},
        {"propertyName": "closed_date", "operator": "GTE", "value": yesterday.isoformat()},
        {"propertyName": "closed_date", "operator": "LT", "value": today.isoformat()},
    ], ["subject", "closed_date"]))
    for cid, ts in ctx.group_tickets(closed).items():
        t = ts[0]
        found.setdefault(cid, []).append({"kind": "ticket_followup", "key": f"ticket:{t['id']}",
                                          "subject": t.get("subject", ""), "detail": f"ticket {t['id']} closed"})
    return {cid: min(ts, key=lambda t: PRIORITY.index(t["kind"])) for cid, ts in found.items()}


def plan(ctx: AgentContext) -> tuple[list[PlannedAction], dict]:
    stats: Counter = Counter()
    found = triggers(ctx)
    stats["triggered"] = len(found)
    cooldown = {r["company_id"] for r in ctx.conn.execute(
        "SELECT DISTINCT company_id FROM ops.ledger WHERE action_type LIKE 'send_%%' AND status = 'done' "
        "AND business_date > %s", (ctx.today - timedelta(days=ctx.cfg.checkin_cooldown_days),))}
    candidates = {}
    for cid, t in found.items():
        if KIND_FOR_TRIGGER[t["kind"]] == "checkin" and cid in cooldown:
            stats["cooldown"] += 1
            continue
        candidates[cid] = t
    # Drop triggers already handled, whichever action they became, before spending a model call.
    keys = {cid: [f"{a}:{cid}:{t['key']}" for a in (ACTION_FOR_KIND[KIND_FOR_TRIGGER[t["kind"]]], "escalate_to_csm")]
            for cid, t in candidates.items()}
    seen = {r["k"] for r in ctx.conn.execute(
        "SELECT action_key AS k FROM ops.ledger WHERE action_key = ANY(%s) "
        "UNION SELECT action_key FROM ops.approvals WHERE action_key = ANY(%s) "
        "UNION SELECT action_key FROM ops.blocked_actions WHERE action_key = ANY(%s) "
        "UNION SELECT action_key FROM ops.shadow_log WHERE action_key = ANY(%s)",
        ([k for ks in keys.values() for k in ks],) * 4)}
    candidates = {cid: t for cid, t in candidates.items() if not seen.intersection(keys[cid])}
    if not candidates:
        return [], dict(stats)
    ids = sorted(candidates)
    companies = ctx.companies(ids)
    contacts = contacts_for(ctx, ids)
    texts = recent_text(ctx, ids, ctx.today - timedelta(days=ctx.cfg.text_window_days))
    usage = warehouse.recent_usage(ctx.conn, ctx.today, 4, ids)
    actions = []
    for cid in ids:
        c, t = companies[cid], candidates[cid]
        weeks = usage.get(cid, [])
        context = {
            "company": {"id": cid, "name": c["name"], "arr": c["arr"], "score": c["score"], "tier": c["tier"],
                        "renewal_date": c["renewal_date"].isoformat() if c["renewal_date"] else None},
            "trigger": {k: v for k, v in t.items() if k != "key"},
            "contacts": contacts.get(cid, []), "recent_text": texts.get(cid, []),
            "figures": {"modules_adopted": weeks[-1].modules if weeks else 0,
                        "weekly_active_users": [w.wau for w in weeks]},
            "csm_name": ctx.owner_name(c["owner_id"]),
        }
        try:
            review = ctx.llm.review_account(context, ["send_checkin", "escalate_to_csm"])
        except LLMError:
            stats["llm_errors"] += 1
            continue
        # Billing is involved when the conversation is about billing; disputes and failed payments are
        # preconditions and block the message outright.
        billing_flag = review.billing_language
        people = {p["id"]: p for p in contacts.get(cid, [])}
        for call in review.tool_calls[:1]:
            try:
                args = validate(call.name, call.arguments)
            except (ValidationError, ValueError):
                stats["rejected_tool_calls"] += 1
                continue
            if call.name == "escalate_to_csm" or review.cancellation_language:
                summary = args.summary if call.name == "escalate_to_csm" else "Cancellation language in recent text."
                actions.append(PlannedAction(
                    "escalate_to_csm", "followup", cid, c["name"], c["owner_id"], t["key"],
                    {"subject": f"Customer mentioned cancelling: {c['name']}", "body": summary, "due_in_days": 1,
                     "priority": "HIGH", "why": f"{t['detail']}; the customer used cancellation language, so nothing "
                                                f"was drafted to them."}, arr=c["arr"]))
                continue
            if args.kind != KIND_FOR_TRIGGER[t["kind"]] or args.contact_id not in people:
                stats["rejected_tool_calls"] += 1
                continue
            actions.append(PlannedAction(
                ACTION_FOR_KIND[args.kind], "followup", cid, c["name"], c["owner_id"], t["key"],
                {"contact_id": args.contact_id, "to_email": people[args.contact_id]["email"], "subject": args.subject,
                 "body": args.body, "why": f"Trigger: {t['detail']}."},
                arr=c["arr"], billing_flag=billing_flag))
    stats["planned"] = len(actions)
    return actions, dict(stats)
