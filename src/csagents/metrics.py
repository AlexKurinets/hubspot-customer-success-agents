"""Every figure the dashboard and the README quote, computed from the ledger and the other ops tables.

The measurement window is the last 61 days (two months) up to the business date. "No human touch" means
the action ran at autonomy level act: nobody approved, edited or prepared it. Renewal lead time is days
from the first time an agent flagged a renewal yellow or red to the renewal date; the before figure is
how many days out a CSM first touched an at-risk renewal in the year before launch.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import psycopg

from . import autonomy
from .config import Config

WINDOW_DAYS = 61


def window(conn: psycopg.Connection) -> tuple[date, date]:
    today = conn.execute("SELECT today FROM ops.clock WHERE id = 1").fetchone()["today"]
    return today - timedelta(days=WINDOW_DAYS - 1), today


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def lead_times(conn: psycopg.Connection, start: date, end: date) -> list[int]:
    rows = conn.execute(
        """WITH flags AS (
               SELECT company_id, payload->>'renewal_date' AS renewal_date, business_date
               FROM ops.ledger WHERE status = 'done' AND payload->>'risk_tier' IN ('yellow', 'red')
                 AND action_type IN ('create_renewal_deal', 'update_renewal_risk'))
           SELECT company_id, renewal_date::date AS renewal_date, min(business_date) AS first_flag FROM flags
           WHERE renewal_date::date BETWEEN %s AND %s GROUP BY 1, 2""", (start, end)).fetchall()
    return [(r["renewal_date"] - r["first_flag"]).days for r in rows]


def summary(conn: psycopg.Connection, cfg: Config) -> dict:
    start, today = window(conn)
    acts = conn.execute("SELECT autonomy, action_type, count(*) AS n FROM ops.ledger WHERE status = 'done' "
                        "AND business_date BETWEEN %s AND %s GROUP BY 1, 2", (start, today)).fetchall()
    total = sum(r["n"] for r in acts)
    by_type: dict[str, dict] = {}
    for r in acts:
        by_type.setdefault(r["action_type"], {"act": 0, "approve": 0})[r["autonomy"]] += r["n"]
    autonomous = sum(r["n"] for r in acts if r["autonomy"] == "act")
    lead = lead_times(conn, start, today)
    before = [r["csm_first_touch_days"] for r in conn.execute(
        "SELECT csm_first_touch_days FROM wh.renewal_history WHERE at_risk AND renewal_date >= %s",
        (cfg.launch_date - timedelta(days=365),))]
    qbr_live = [r["minutes"] for r in conn.execute(
        "SELECT minutes FROM ops.time_logs WHERE activity = 'qbr_prep' AND source = 'live' AND logged_on BETWEEN %s AND %s",
        (start, today))]
    qbr_before = [r["minutes"] for r in conn.execute(
        "SELECT minutes FROM ops.time_logs WHERE activity = 'qbr_prep' AND source = 'history'")]
    tiers = {r["tier"]: r["n"] for r in conn.execute(
        "SELECT tier, count(*) AS n FROM (SELECT DISTINCT ON (company_id) tier FROM ops.health_scores "
        "ORDER BY company_id, business_date DESC) t GROUP BY 1")}
    return {
        "today": today.isoformat(), "launch_date": cfg.launch_date.isoformat(),
        "window": {"start": start.isoformat(), "end": today.isoformat(), "days": WINDOW_DAYS},
        "accounts": sum(tiers.values()), "tiers": tiers,
        "actions": {"total": total, "per_month": round(total * 30.44 / WINDOW_DAYS),
                    "autonomous": autonomous, "autonomous_share": round(autonomous / total, 3) if total else None,
                    "by_type": by_type},
        "renewal_lead_days": {"after_median": _median(lead), "after_n": len(lead),
                              "before_median": _median(before), "before_n": len(before)},
        "qbr_prep_minutes": {"after_median": _median(qbr_live), "after_n": len(qbr_live),
                             "before_median": _median(qbr_before), "before_n": len(qbr_before)},
        "health_model": {"points": cfg.weights, "tiers": cfg.tiers, **cfg.weights_meta},
        "approvals_open": conn.execute("SELECT count(*) AS n FROM ops.approvals WHERE status = 'open'").fetchone()["n"],
        "graduation": graduation(conn, cfg),
    }


def graduation(conn: psycopg.Connection, cfg: Config) -> list[dict]:
    out = []
    for name, spec in cfg.actions.items():
        if not spec.graduates:
            continue
        g = autonomy.graduation(conn, cfg, name)
        first = conn.execute("SELECT min(business_date) AS d FROM ops.graduations WHERE action_type = %s AND graduated",
                             (name,)).fetchone()["d"]
        out.append({"action_type": name, "graduated": g.graduated, "last_decisions": g.decisions,
                    "edited_or_rejected": g.edited_or_rejected,
                    "graduated_on": first.isoformat() if first else None,
                    "days_after_launch": (first - cfg.launch_date).days if first else None})
    return out


def monthly(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT to_char(business_date, 'YYYY-MM') AS month, action_type, autonomy, count(*) AS n FROM ops.ledger "
        "WHERE status = 'done' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3").fetchall()
    return [dict(r) for r in rows]


def reliability(conn: psycopg.Connection) -> dict:
    start, today = window(conn)
    client = conn.execute(
        "SELECT COALESCE(sum(requests), 0) AS requests, COALESCE(sum(retries), 0) AS retries, "
        "COALESCE(sum(rate_limited), 0) AS rate_limited, COALESCE(sum(server_errors), 0) AS server_errors, "
        "COALESCE(sum(throttle_waits), 0) AS throttle_waits FROM ops.client_stats").fetchone()
    week = today - timedelta(days=6)
    return {
        "client": dict(client),
        "ledger": {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) AS n FROM ops.ledger GROUP BY 1")},
        "collisions": conn.execute("SELECT count(*) AS n FROM ops.ledger_collisions").fetchone()["n"],
        "collisions_by_source": {r["source"]: r["n"] for r in conn.execute(
            "SELECT source, count(*) AS n FROM ops.ledger_collisions GROUP BY 1")},
        "webhooks": {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM ops.webhook_events GROUP BY 1")},
        "webhook_duplicates": conn.execute("SELECT count(*) AS n FROM ops.webhook_duplicates").fetchone()["n"],
        "webhook_rejections": conn.execute("SELECT count(*) AS n FROM ops.webhook_rejections").fetchone()["n"],
        "dead_letters_open": conn.execute("SELECT count(*) AS n FROM ops.dead_letters WHERE replayed_at IS NULL").fetchone()["n"],
        "failed_runs": [dict(r) for r in conn.execute(
            "SELECT thread_id, error FROM ops.runs WHERE status = 'failed' ORDER BY business_date DESC LIMIT 20")],
        "blocked_last_7_days": [dict(r) for r in conn.execute(
            "SELECT b.business_date, b.action_type, b.company_id, b.precondition, b.detail FROM ops.blocked_actions b "
            "WHERE b.business_date BETWEEN %s AND %s ORDER BY b.business_date DESC, b.id DESC", (week, today))],
        "blocked_in_window": {r["precondition"]: r["n"] for r in conn.execute(
            "SELECT precondition, count(*) AS n FROM ops.blocked_actions WHERE business_date BETWEEN %s AND %s GROUP BY 1",
            (start, today))},
        "cancelled_approvals": [dict(r) for r in conn.execute(
            "SELECT id, action_type, company_name, outcome, decided_on FROM ops.approvals WHERE status = 'cancelled' "
            "ORDER BY id DESC LIMIT 20")],
        "shadow_week": {r["action_type"]: r["n"] for r in conn.execute(
            "SELECT action_type, count(*) AS n FROM ops.shadow_log GROUP BY 1")},
        "qbr_checks": {r["check_status"]: r["n"] for r in conn.execute(
            "SELECT check_status, count(*) AS n FROM ops.qbr_drafts GROUP BY 1")},
    }
