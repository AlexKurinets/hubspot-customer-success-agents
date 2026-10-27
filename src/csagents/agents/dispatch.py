"""Where every planned action goes after an agent proposes it: drop what is already done, apply the
preconditions, then shadow-log it, run it, or send it for approval. One path for all three agents."""

from __future__ import annotations

from collections import Counter

from psycopg.types.json import Jsonb

from .. import approvals, autonomy, ledger, preconditions
from ..actions import PlannedAction
from ..executor import Executor
from .context import AgentContext


def dispatch(ctx: AgentContext, actions: list[PlannedAction]) -> Counter:
    stats: Counter = Counter()
    if not actions:
        return stats
    keys = [a.key for a in actions]
    done = ledger.done_before(ctx.conn, keys, ctx.today)
    asked = {r["action_key"] for r in ctx.conn.execute(
        "SELECT action_key FROM ops.approvals WHERE action_key = ANY(%s)", (keys,))}
    graduated = ctx.cache.get("graduated")
    if graduated is None:
        graduated = ctx.cache["graduated"] = autonomy.refresh_graduations(ctx.conn, ctx.cfg, ctx.today)
    executor = Executor(ctx.conn, ctx.crm, ctx.today, ctx.source)
    bills = ctx.billing()
    for a in actions:
        if a.key in done or a.key in asked:
            stats["already_handled"] += 1
            continue
        level, reason = autonomy.decide(ctx.cfg, a, graduated)
        if level == "never":
            stats["never"] += 1
            continue
        block = preconditions.check(a, bills.get(a.company_id))
        if block:
            preconditions.record_block(ctx.conn, a, ctx.today, *block)
            stats["blocked"] += 1
            continue
        if ctx.shadow:
            ctx.conn.execute(
                "INSERT INTO ops.shadow_log (business_date, agent, action_type, company_id, action_key, "
                "would_need_approval, payload) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (action_key) DO NOTHING",
                (ctx.today, a.agent, a.action_type, a.company_id, a.key, level == "approve", Jsonb(a.payload)))
            ctx.conn.commit()
            stats["shadow"] += 1
            continue
        if level == "act":
            stats[executor.run(a, "act")] += 1
        else:
            stats[f"approval_{approvals.request(ctx, a, reason)}"] += 1
    return stats
