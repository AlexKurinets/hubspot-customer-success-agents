"""Reads the agents hand to the model: an account's contacts and its recent inbound text."""

from __future__ import annotations

from datetime import date

from .context import AgentContext

ROLE_ORDER = {"practice_manager": 0, "lead_veterinarian": 1, "office_admin": 2}


def contacts_for(ctx: AgentContext, company_ids: list[int]) -> dict[int, list[dict]]:
    links = ctx.crm.associations("companies", "contacts", company_ids)
    ids = sorted({c for cs in links.values() for c in cs})
    objs = {int(o["id"]): o["properties"] for o in
            ctx.crm.batch_read("contacts", ids, ["firstname", "lastname", "email", "cs_role"])}
    out = {}
    for cid, cs in links.items():
        people = [{"id": x, "name": f"{objs[x].get('firstname', '')} {objs[x].get('lastname', '')}".strip(),
                   "email": objs[x].get("email"), "role": objs[x].get("cs_role")} for x in cs if x in objs]
        out[cid] = sorted(people, key=lambda p: (ROLE_ORDER.get(p["role"], 9), p["id"]))
    return out


def recent_text(ctx: AgentContext, company_ids: list[int], since: date) -> dict[int, list[dict]]:
    """Ticket text and inbound emails since `since`, oldest first. This is customer data: the model is told
    to treat it as content to read, never as instructions."""
    out: dict[int, list[dict]] = {cid: [] for cid in company_ids}
    t_links = ctx.crm.associations("companies", "tickets", company_ids)
    e_links = ctx.crm.associations("companies", "emails", company_ids)
    t_ids = sorted({t for ts in t_links.values() for t in ts})
    e_ids = sorted({e for es in e_links.values() for e in es})
    tickets = {int(o["id"]): o["properties"] for o in
               ctx.crm.batch_read("tickets", t_ids, ["subject", "content", "createdate"])}
    emails = {int(o["id"]): o["properties"] for o in
              ctx.crm.batch_read("emails", e_ids, ["hs_email_subject", "hs_email_text", "hs_email_direction", "hs_timestamp"])}
    cutoff = since.isoformat()
    for cid in company_ids:
        for t in t_links.get(cid, []):
            p = tickets.get(t)
            if p and (p.get("createdate") or "") >= cutoff:
                out[cid].append({"source": "ticket", "date": p["createdate"][:10],
                                 "text": f"{p.get('subject', '')}. {p.get('content', '')}"})
        for e in e_links.get(cid, []):
            p = emails.get(e)
            if p and p.get("hs_email_direction") == "INCOMING_EMAIL" and (p.get("hs_timestamp") or "") >= cutoff:
                out[cid].append({"source": "email", "date": p["hs_timestamp"][:10],
                                 "text": f"{p.get('hs_email_subject', '')}. {p.get('hs_email_text', '')}"})
        out[cid].sort(key=lambda x: x["date"])
    return out
