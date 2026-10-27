"""Approval flow: one LangGraph thread per action that needs a person.

request -> wait -> decide. `wait` calls interrupt(), and the Postgres checkpointer keeps the thread
until a CSM answers, which can be days later and from a different process. On resume, `decide`
re-checks the world before acting: billing preconditions are read again, and a change to a field people
own is cancelled if that field no longer holds the value the agent saw when it asked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.types.json import Jsonb

from . import preconditions
from .actions import PlannedAction
from .executor import Executor
from .hubspot.client import HubSpotError
from .sources import billing

if TYPE_CHECKING:
    from .agents.context import AgentContext

OUTCOME = {"done": "executed", "duplicate": "cancelled", "failed": "failed", "dead": "failed"}


class ApprovalState(TypedDict, total=False):
    action: dict
    reason: str
    approval_id: int
    decision: dict
    outcome: str


def thread_id(action_key: str) -> str:
    return f"approval:{action_key}"


def _finish(ctx: AgentContext, approval_id: int, status: str, outcome: str) -> None:
    ctx.conn.execute("UPDATE ops.approvals SET status = %s, outcome = %s WHERE id = %s", (status, outcome, approval_id))
    ctx.conn.commit()


def stale_human_edit(ctx: AgentContext, action: PlannedAction) -> str | None:
    """For an approved change to a human-owned field: is the field still what the agent saw when it asked?
    Values are compared rather than timestamps, so no two clocks have to agree. The property history
    names whoever changed it."""
    field = ctx.cfg.actions[action.action_type].human_field
    if not field:
        return None
    deal = ctx.crm.get("deals", int(action.payload["deal_id"]), [field], history=[field])
    current = float(deal["properties"].get(field) or 0)
    if current == float(action.payload["new_amount"]):
        return f"{field} is already at the proposed value"
    if current != float(action.payload["old_amount"]):
        latest = (deal.get("propertiesWithHistory", {}).get(field) or [{}])[0]
        return f"{field} was edited by {latest.get('sourceId', 'someone')} after approval was requested"
    return None


def build(ctx: AgentContext):
    def request(state: ApprovalState) -> dict:
        a = PlannedAction.from_dict(state["action"])
        row = ctx.conn.execute(
            """INSERT INTO ops.approvals (action_key, action_type, agent, company_id, company_name, owner_id, reason,
                                          requested_on, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (action_key) DO NOTHING RETURNING id""",
            (a.key, a.action_type, a.agent, a.company_id, a.company_name, a.owner_id, state["reason"], ctx.today,
             Jsonb(a.payload))).fetchone()
        ctx.conn.commit()
        return {"approval_id": row["id"]} if row else {"outcome": "already_requested"}

    def wait(state: ApprovalState) -> dict:
        # The Slack message is the approvals row; the graph sleeps here until a decision resumes it.
        return {"decision": interrupt({"approval_id": state["approval_id"]})}

    def decide(state: ApprovalState) -> dict:
        d, approval_id = state["decision"], state["approval_id"]
        a = PlannedAction.from_dict(state["action"])
        if d["decision"] == "reject":
            _finish(ctx, approval_id, "rejected", f"rejected by {d['by']}")
            return {"outcome": "rejected"}
        if d.get("payload"):
            a.payload = {**a.payload, **d["payload"]}
        block = preconditions.check(a, billing.states(ctx.conn, ctx.today, [a.company_id]).get(a.company_id))
        if block:
            preconditions.record_block(ctx.conn, a, ctx.today, *block)
            _finish(ctx, approval_id, "cancelled", f"blocked at execution: {block[1]}")
            return {"outcome": "cancelled"}
        try:
            stale = stale_human_edit(ctx, a)
        except HubSpotError as e:
            stale = f"could not re-read the record: {e.message}"
        if stale:
            _finish(ctx, approval_id, "cancelled", stale)
            return {"outcome": "cancelled"}
        status = Executor(ctx.conn, ctx.crm, ctx.today, "approval").run(a, "approve", approval_id, approved_by=d["by"])
        _finish(ctx, approval_id, OUTCOME[status], {"done": f"approved by {d['by']}", "duplicate": "already done"}.get(
            status, f"execution {status}"))
        return {"outcome": OUTCOME[status]}

    g = StateGraph(ApprovalState)
    g.add_node("request", request)
    g.add_node("wait", wait)
    g.add_node("decide", decide)
    g.add_edge(START, "request")
    g.add_conditional_edges("request", lambda s: END if s.get("outcome") else "wait", ["wait", END])
    g.add_edge("wait", "decide")
    g.add_edge("decide", END)
    return g.compile(checkpointer=PostgresSaver(ctx.approval_saver_conn or ctx.saver_conn))


def _graph(ctx: AgentContext):
    if "approval_graph" not in ctx.cache:
        ctx.cache["approval_graph"] = build(ctx)
    return ctx.cache["approval_graph"]


def request(ctx: AgentContext, action: PlannedAction, reason: str) -> str:
    out = _graph(ctx).invoke({"action": action.to_dict(), "reason": reason},
                             {"configurable": {"thread_id": thread_id(action.key)}}, durability="exit")
    return out.get("outcome") or "requested"


def resume_decided(ctx: AgentContext) -> dict:
    """The worker's half of an approval: resume every thread a CSM has answered."""
    rows = ctx.conn.execute("SELECT * FROM ops.approvals WHERE status IN ('approved', 'edited', 'rejected') "
                            "AND outcome IS NULL ORDER BY id").fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        decision = {"decision": r["decision"], "by": r["decided_by"], "payload": r["edited_payload"]}
        out = _graph(ctx).invoke(Command(resume=decision), {"configurable": {"thread_id": thread_id(r["action_key"])}},
                                 durability="exit")
        counts[out.get("outcome", "unknown")] = counts.get(out.get("outcome", "unknown"), 0) + 1
    return counts


def decide(conn, approval_id: int, decision: str, by: str, today, payload: dict | None = None,
           review_minutes: int | None = None) -> bool:
    """Records a CSM's answer (what the Slack button handler did). The worker carries it out."""
    status = {"approve": "approved", "edit": "edited", "reject": "rejected"}[decision]
    n = conn.execute(
        "UPDATE ops.approvals SET decision = %s, status = %s, edited_payload = %s, decided_by = %s, decided_on = %s, "
        "review_minutes = %s WHERE id = %s AND status = 'open'",
        (decision, status, Jsonb(payload) if payload else None, by, today, review_minutes, approval_id)).rowcount
    conn.commit()
    return n == 1
