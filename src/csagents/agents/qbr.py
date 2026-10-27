"""QBR preparation agent.

Seven days before an account's quarterly review, code assembles the figures from the warehouse, HubSpot
and billing, and the model writes the narrative around them. A check then compares every number in the
draft with the figures. A mismatch gets one regeneration with the offending numbers named; a second
mismatch sends the draft to the CSM flagged, with those numbers listed on the approval.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from psycopg.types.json import Jsonb

from ..actions import PlannedAction
from ..llm.base import LLMError
from ..sources import warehouse
from .context import AgentContext

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
CONSTANTS = {"3", "90"}  # the number of core modules and the ticket window, which the narrative may name


def figures_for(ctx: AgentContext, c: dict) -> dict:
    weeks = warehouse.recent_usage(ctx.conn, ctx.today, 26, [c["id"]]).get(c["id"], [])
    this_q, last_q = weeks[-13:], weeks[:-13]
    avg = round(sum(w.wau for w in this_q) / len(this_q)) if this_q else 0
    prev = sum(w.wau for w in last_q) / len(last_q) if last_q else 0
    seats = this_q[-1].seats if this_q else c["seats"]
    tickets = ctx.tickets_since(ctx.today - timedelta(days=90)).get(c["id"], [])
    b = ctx.billing().get(c["id"])
    renewal = c["renewal_date"]
    return {
        "active_users_avg": avg,
        "active_users_change_pct": round(100 * (avg - prev) / prev) if prev else 0,
        "seats": seats,
        "seat_utilization_pct": round(100 * avg / seats) if seats else 0,
        "modules_adopted": this_q[-1].modules if this_q else 0,
        "tickets_opened": len(tickets),
        "tickets_high": sum(1 for t in tickets if t.get("hs_ticket_priority") == "HIGH"),
        "arr": round(b.arr if b else c["arr"]),
        "renewal_date": renewal.isoformat() if renewal else "",
        "days_to_renewal": (renewal - ctx.today).days if renewal else 0,
    }


def check(text: str, figures: dict) -> list[str]:
    """Every number in the narrative must be one of the figures (or a named constant)."""
    allowed = set(CONSTANTS)
    for v in figures.values():
        if isinstance(v, int | float):
            allowed.add(str(abs(int(v))) if float(v).is_integer() else str(abs(v)))
    found = NUMBER.findall(ISO_DATE.sub(" ", text))
    return sorted({n for n in found if n.replace(",", "").rstrip(".") not in allowed})


def plan(ctx: AgentContext) -> tuple[list[PlannedAction], dict]:
    stats: Counter = Counter()
    target = ctx.today + timedelta(days=ctx.cfg.qbr_prepare_days)
    ids = warehouse.company_ids(ctx.conn)
    due = [c for c in ctx.companies(ids).values() if c["next_qbr"] == target and c["arr"] >= ctx.cfg.qbr_min_arr]
    actions = []
    for c in due:
        key_trigger = f"qbr:{target.isoformat()}"
        key = f"prepare_qbr:{c['id']}:{key_trigger}"
        if ctx.conn.execute("SELECT 1 FROM ops.qbr_drafts WHERE action_key = %s", (key,)).fetchone():
            continue
        figures = figures_for(ctx, c)
        feedback: list[str] = []
        text, attempts = "", 0
        try:
            for attempts in range(1, ctx.cfg.qbr_max_attempts + 1):
                text = ctx.llm.write_qbr({"id": c["id"], "name": c["name"], "meeting_date": target.isoformat()},
                                         figures, attempts, feedback)
                feedback = check(text, figures)
                if not feedback:
                    break
        except LLMError:
            stats["llm_errors"] += 1
            continue
        status = "flagged" if feedback else ("passed" if attempts == 1 else "regenerated")
        stats[status] += 1
        draft_id = ctx.conn.execute(
            "INSERT INTO ops.qbr_drafts (company_id, meeting_date, business_date, figures, narrative, attempts, "
            "check_status, mismatches, action_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (c["id"], target, ctx.today, Jsonb(figures), text, attempts, status, Jsonb(feedback), key)).fetchone()["id"]
        ctx.conn.commit()
        flagged = f" Figure check flagged: {', '.join(feedback)}." if feedback else ""
        actions.append(PlannedAction(
            "prepare_qbr", "qbr", c["id"], c["name"], c["owner_id"], key_trigger,
            {"draft_id": draft_id, "meeting_date": target.isoformat(), "check_status": status, "mismatches": feedback,
             "why": f"QBR on {target.isoformat()}; draft {draft_id} ready for review.{flagged}"},
            arr=c["arr"]))
    stats["planned"] = len(actions)
    return actions, dict(stats)
