"""Health and renewal-risk agent.

Nightly: score every account from usage, tickets and billing; write the score and tier to cs_agent_
properties in batches of 100; open a renewal deal 120 days before contract end with the current risk
tier; keep that deal's risk tier current; propose a seat-change amount update (approval only); open an
expansion task after three weeks above 90% seat utilization. When an account drops a tier, the model
reads its recent ticket and email text, tags the reasons and drafts the recovery task.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from psycopg.types.json import Jsonb

from .. import scoring
from ..actions import PlannedAction
from ..llm.base import LLMError
from ..sources import warehouse
from ..tools import ValidationError, validate
from .context import AgentContext
from .text import contacts_for, recent_text

RANK = {"green": 0, "yellow": 1, "red": 2}
DEAL_PROPS = ["amount", "closedate", "cs_agent_risk_tier", "dealstage"]


def quarter(day: date) -> str:
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def renewal_deals(ctx: AgentContext, company_ids: list[int] | None = None) -> dict[int, dict]:
    """The renewal deal each company has from the agents, read back from HubSpot for current values."""
    sql = ("SELECT DISTINCT ON (company_id) company_id, hubspot_id, payload FROM ops.ledger "
           "WHERE action_type = 'create_renewal_deal' AND status = 'done'"
           + (" AND company_id = ANY(%s)" if company_ids is not None else "")
           + " ORDER BY company_id, business_date DESC, id DESC")
    rows = ctx.conn.execute(sql, [company_ids] if company_ids is not None else []).fetchall()
    open_rows = [r for r in rows if date.fromisoformat(r["payload"]["renewal_date"]) > ctx.today]
    current = {int(o["id"]): o["properties"] for o in
               ctx.crm.batch_read("deals", [r["hubspot_id"] for r in open_rows], DEAL_PROPS)}
    out = {}
    for r in open_rows:
        p = current.get(r["hubspot_id"])
        if p is None:
            continue
        out[r["company_id"]] = {"deal_id": r["hubspot_id"], "renewal_date": r["payload"]["renewal_date"],
                                "amount": float(p.get("amount") or 0), "risk_tier": p.get("cs_agent_risk_tier")}
    return out


def plan(ctx: AgentContext, company_ids: list[int] | None = None, renewal_only: bool = False) -> tuple[list, dict]:
    cfg, today, conn = ctx.cfg, ctx.today, ctx.conn
    ids = company_ids if company_ids is not None else warehouse.company_ids(conn)
    companies = ctx.companies(ids)
    usage = warehouse.recent_usage(conn, today, cfg.usage_weeks, company_ids)
    bills = ctx.billing()
    since = today - timedelta(days=cfg.ticket_window_days)
    tickets = ctx.tickets_since(since) if company_ids is None else _tickets_for(ctx, ids, since)
    deals = renewal_deals(ctx, company_ids)
    stats: Counter = Counter()
    actions: list[PlannedAction] = []
    updates: list[tuple[int, dict]] = []
    to_review = []

    for cid in ids:
        c = companies.get(cid)
        if c is None:
            continue
        b = bills.get(cid)
        h = scoring.score(scoring.features(usage.get(cid, []), [t.get("hs_ticket_priority") or "LOW"
                                                                for t in tickets.get(cid, [])], b), cfg.weights, cfg.tier)
        c["health"] = h
        if not renewal_only:
            if (c["score"], c["tier"]) != (h.score, h.tier):
                updates.append((cid, {"cs_agent_health_score": str(h.score), "cs_agent_health_tier": h.tier,
                                      "cs_agent_reviewed_at": today.isoformat()}))
                conn.execute("INSERT INTO ops.health_scores (company_id, business_date, score, tier, features, points) "
                             "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (company_id, business_date) DO UPDATE SET "
                             "score = EXCLUDED.score, tier = EXCLUDED.tier, features = EXCLUDED.features, "
                             "points = EXCLUDED.points", (cid, today, h.score, h.tier, Jsonb(h.features), Jsonb(h.points)))
            if c["tier"] and RANK[h.tier] > RANK[c["tier"]]:
                c["tier_before"] = c["tier"]
                to_review.append(c)
            weeks = usage.get(cid, [])[-cfg.expansion_weeks:]
            if len(weeks) == cfg.expansion_weeks and all(w.utilization > cfg.expansion_threshold for w in weeks):
                actions.append(PlannedAction(
                    "create_expansion_task", "health", cid, c["name"], c["owner_id"], f"expansion:{quarter(today)}",
                    {"subject": f"Seat utilization above {cfg.expansion_threshold:.0%} for {cfg.expansion_weeks} weeks: "
                                f"{c['name']}",
                     "body": "Weekly active users against licensed seats: "
                             + ", ".join(f"{w.week_start}: {w.wau}/{w.seats}" for w in weeks)
                             + ". Most seat additions in the history followed this pattern.",
                     "due_in_days": 7, "assignee_id": c["account_manager_id"],
                     "why": f"Seat utilization stayed above {cfg.expansion_threshold:.0%} for {cfg.expansion_weeks} weeks."},
                    arr=c["arr"]))

        rd = c["renewal_date"]
        if rd is None or not 0 < (rd - today).days <= cfg.renewal_open_days:
            continue
        deal = deals.get(cid)
        arr = round(b.arr if b else c["arr"])
        if deal is None or deal["renewal_date"] != rd.isoformat():
            actions.append(PlannedAction(
                "create_renewal_deal", "health", cid, c["name"], c["owner_id"], f"renewal:{rd.isoformat()}",
                {"renewal_date": rd.isoformat(), "amount": arr, "risk_tier": h.tier,
                 "why": f"Contract ends {rd.isoformat()}, {(rd - today).days} days out. Health {h.score} ({h.tier})."},
                arr=c["arr"]))
            continue
        if deal["risk_tier"] != h.tier:
            actions.append(PlannedAction(
                "update_renewal_risk", "health", cid, c["name"], c["owner_id"], f"risk:{rd.isoformat()}:{today.isoformat()}",
                {"deal_id": deal["deal_id"], "renewal_date": rd.isoformat(), "risk_tier": h.tier,
                 "previous_tier": deal["risk_tier"], "score": h.score,
                 "why": f"Health moved from {deal['risk_tier']} to {h.tier} ({h.score})."},
                arr=c["arr"]))
        if not renewal_only and deal["amount"] and abs(deal["amount"] - arr) >= 1:
            actions.append(PlannedAction(
                "change_deal_amount", "health", cid, c["name"], c["owner_id"], f"amount:{rd.isoformat()}:{arr}",
                {"deal_id": deal["deal_id"], "old_amount": deal["amount"], "new_amount": arr,
                 "why": f"Billing shows annual value {arr:,} against {deal['amount']:,.0f} on the renewal deal."},
                arr=c["arr"]))

    if updates:
        ctx.crm.batch_update("companies", updates)
        for cid, props in updates:
            companies[cid]["score"], companies[cid]["tier"] = int(props["cs_agent_health_score"]), props["cs_agent_health_tier"]
        stats["scores_written"] = len(updates)
    conn.commit()
    if to_review:
        actions.extend(_review(ctx, to_review, stats))
    stats["planned"] = len(actions)
    return actions, dict(stats)


def _tickets_for(ctx: AgentContext, ids: list[int], since: date) -> dict[int, list[dict]]:
    links = ctx.crm.associations("companies", "tickets", ids)
    all_ids = [t for ts in links.values() for t in ts]
    objs = {int(o["id"]): o["properties"] for o in ctx.crm.batch_read("tickets", all_ids, ["hs_ticket_priority", "createdate"])}
    return {cid: [{"id": t, **objs[t]} for t in ts if t in objs and (objs[t].get("createdate") or "") >= since.isoformat()]
            for cid, ts in links.items()}


def _review(ctx: AgentContext, accounts: list[dict], stats: Counter) -> list[PlannedAction]:
    """A tier drop: the model tags why from the account's recent text and drafts the recovery task."""
    texts = recent_text(ctx, [c["id"] for c in accounts], ctx.today - timedelta(days=ctx.cfg.text_window_days))
    contacts = contacts_for(ctx, [c["id"] for c in accounts])
    out, reasons_update = [], []
    for c in accounts:
        h = c["health"]
        context = {
            "company": {"id": c["id"], "name": c["name"], "arr": c["arr"], "score": h.score, "tier": h.tier,
                        "points": h.points, "weights": ctx.cfg.weights,
                        "renewal_date": c["renewal_date"].isoformat() if c["renewal_date"] else None},
            "trigger": {"kind": "tier_drop", "from": c.get("tier_before") or "", "to": h.tier},
            "contacts": contacts.get(c["id"], []), "recent_text": texts.get(c["id"], []),
            "figures": h.features, "csm_name": ctx.owner_name(c["owner_id"]),
        }
        try:
            review = ctx.llm.review_account(context, ["create_task"])
        except LLMError:
            stats["llm_errors"] += 1
            continue
        codes = ",".join(r.code for r in review.reasons)
        reasons_update.append((c["id"], {"cs_agent_health_reason": "; ".join(f"{r.code}: {r.evidence}" for r in review.reasons)
                                         or "no reason found in recent text"}))
        ctx.conn.execute("UPDATE ops.health_scores SET reasons = %s WHERE company_id = %s AND business_date = %s",
                         ([r.code for r in review.reasons], c["id"], ctx.today))
        for call in review.tool_calls:
            try:
                args = validate(call.name, call.arguments)
            except (ValidationError, ValueError):
                stats["rejected_tool_calls"] += 1
                continue
            if call.name != "create_task":
                stats["rejected_tool_calls"] += 1
                continue
            week = ctx.today - timedelta(days=ctx.today.weekday())
            out.append(PlannedAction(
                "open_recovery_task", "health", c["id"], c["name"], c["owner_id"], f"tier_drop:{h.tier}:{week.isoformat()}",
                {"subject": args.subject, "body": args.body, "due_in_days": args.due_in_days, "reasons": codes,
                 "priority": "HIGH" if h.tier == "red" else "MEDIUM",
                 "why": f"Health dropped to {h.tier} ({h.score}). Reasons: {codes or 'none stated'}."},
                arr=c["arr"]))
    ctx.conn.commit()
    if reasons_update:
        ctx.crm.batch_update("companies", reasons_update)
    stats["reviewed"] = len(accounts)
    return out
