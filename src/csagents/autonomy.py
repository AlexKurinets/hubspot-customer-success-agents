"""Which actions run on their own and which wait for a CSM.

Every action type has a level in config/agents.yaml: act, approve or never. Customer-facing actions
always wait when the account is over the ARR threshold or billing is involved. A graduating action type
starts at approve and runs on its own once the CSMs' last 50 decisions on it had under 5% edited or
rejected. The same rule demotes it again if the remaining forced approvals start getting edited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

from .actions import PlannedAction
from .config import Config


@dataclass(frozen=True)
class Graduation:
    action_type: str
    decisions: int
    edited_or_rejected: int
    window: int
    max_rate: float

    @property
    def rate(self) -> float | None:
        return self.edited_or_rejected / self.decisions if self.decisions else None

    @property
    def graduated(self) -> bool:
        return self.decisions >= self.window and self.edited_or_rejected / self.decisions < self.max_rate


def graduation(conn: psycopg.Connection, cfg: Config, action_type: str) -> Graduation:
    rows = conn.execute(
        "SELECT decision FROM ops.approvals WHERE action_type = %s AND decision IS NOT NULL "
        "ORDER BY decided_on DESC, id DESC LIMIT %s", (action_type, cfg.graduation_window)).fetchall()
    bad = sum(1 for r in rows if r["decision"] != "approve")
    return Graduation(action_type, len(rows), bad, cfg.graduation_window, cfg.graduation_max_edit_rate)


def refresh_graduations(conn: psycopg.Connection, cfg: Config, today: date) -> dict[str, bool]:
    """Evaluates every graduating type once per run and records any change of state."""
    out = {}
    for name, spec in cfg.actions.items():
        if not spec.graduates:
            continue
        g = graduation(conn, cfg, name)
        last = conn.execute("SELECT graduated FROM ops.graduations WHERE action_type = %s ORDER BY id DESC LIMIT 1",
                            (name,)).fetchone()
        if (last["graduated"] if last else False) != g.graduated:
            conn.execute("INSERT INTO ops.graduations (action_type, business_date, graduated, decisions, edited_or_rejected) "
                         "VALUES (%s, %s, %s, %s, %s)", (name, today, g.graduated, g.decisions, g.edited_or_rejected))
        out[name] = g.graduated
    conn.commit()
    return out


def decide(cfg: Config, action: PlannedAction, graduated: dict[str, bool]) -> tuple[str, str]:
    """Returns (level, reason). The reason is what the CSM sees on the approval request."""
    spec = cfg.actions[action.action_type]
    if spec.autonomy == "never":
        return "never", "this action is never automated"
    if spec.customer_facing:
        if action.arr > cfg.arr_threshold:
            return "approve", f"account ARR {action.arr:,.0f} is over the {cfg.arr_threshold:,} approval threshold"
        if action.billing_flag:
            return "approve", "the account has a billing issue or the message touches billing"
    if spec.autonomy == "approve":
        if spec.graduates and graduated.get(action.action_type):
            return "act", ""
        if spec.graduates:
            return "approve", "this action type has not yet met the graduation rule"
        if spec.human_field:
            return "approve", f"changes {spec.human_field}, a field people own"
        return "approve", "CSM sign-off required"
    return "act", ""
