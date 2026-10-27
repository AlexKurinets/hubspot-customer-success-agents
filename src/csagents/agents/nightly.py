"""Each agent's nightly run is a LangGraph graph (plan -> act -> record) with a Postgres checkpointer and
one thread per agent per business date. If HubSpot stays down long enough to exhaust the client's
retries, the run stops at that node, is marked failed, and `csagents replay --run <thread>` resumes it
from the last completed node. Re-running a node is safe because every write goes through the ledger.
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.types.json import Jsonb

from ..actions import PlannedAction
from ..hubspot.client import HubSpotUnavailable
from . import followup, health, qbr
from .context import AgentContext
from .dispatch import dispatch

AGENTS = {"health": health.plan, "followup": followup.plan, "qbr": qbr.plan}
ORDER = ["health", "followup", "qbr"]


class RunState(TypedDict, total=False):
    agent: str
    date: str
    planned: list[dict]
    stats: dict


def build(ctx: AgentContext, agent: str):
    def plan(state: RunState) -> dict:
        actions, stats = AGENTS[agent](ctx)
        return {"planned": [a.to_dict() for a in actions], "stats": stats}

    def act(state: RunState) -> dict:
        hook = ctx.cache.pop("before_act", None)
        if hook:
            hook()
        counts = dispatch(ctx, [PlannedAction.from_dict(d) for d in state["planned"]])
        return {"stats": {**state["stats"], **{f"dispatch_{k}": v for k, v in counts.items()}}}

    def record(state: RunState) -> dict:
        ctx.conn.execute("UPDATE ops.runs SET status = 'done', stats = %s, finished_at = now() WHERE thread_id = %s",
                         (Jsonb(state["stats"]), thread(agent, ctx)))
        ctx.conn.commit()
        return {}

    g = StateGraph(RunState)
    g.add_node("plan", plan)
    g.add_node("act", act)
    g.add_node("record", record)
    g.add_edge(START, "plan")
    g.add_edge("plan", "act")
    g.add_edge("act", "record")
    g.add_edge("record", END)
    return g.compile(checkpointer=PostgresSaver(ctx.saver_conn))


def thread(agent: str, ctx: AgentContext) -> str:
    return f"{agent}:{ctx.today.isoformat()}"


def run_agent(ctx: AgentContext, agent: str, resume: bool = False) -> dict:
    tid = thread(agent, ctx)
    ctx.conn.execute("INSERT INTO ops.runs (thread_id, agent, business_date, status) VALUES (%s, %s, %s, 'running') "
                     "ON CONFLICT (thread_id) DO UPDATE SET status = 'running', error = NULL", (tid, agent, ctx.today))
    ctx.conn.commit()
    graph = build(ctx, agent)
    config = {"configurable": {"thread_id": tid}}
    try:
        out = graph.invoke(None if resume else {"agent": agent, "date": ctx.today.isoformat()}, config, durability="sync")
    except HubSpotUnavailable as e:
        ctx.conn.rollback()
        ctx.conn.execute("UPDATE ops.runs SET status = 'failed', error = %s, finished_at = now() WHERE thread_id = %s",
                         (str(e)[:500], tid))
        ctx.conn.commit()
        return {"agent": agent, "status": "failed", "error": str(e)}
    return {"agent": agent, "status": "done", **(out.get("stats") or {})}


def run_all(ctx: AgentContext) -> list[dict]:
    return [run_agent(ctx, a) for a in ORDER]
