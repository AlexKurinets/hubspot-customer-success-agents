"""Drives the stack one business day at a time: the world changes, HubSpot delivers its webhooks, the
worker handles them, the three agents run their nightly graphs, simulated CSMs answer approvals, and the
worker resumes the answered threads. The agents run exactly as they do live; only the people are simulated.

Simulated CSMs: each open approval is answered the same day about half the time and within two days
always. Drafts are edited or rejected about a quarter of the time for the first four weeks, a tenth in
week five after the templates are revised from those edits, and one in fifty after that, which is what
lets a draft type pass the graduation rule once enough of the newer drafts have been decided.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from datetime import date, timedelta

import psycopg

from .. import approvals, db
from ..agents import jobs, nightly
from ..agents.context import AgentContext
from ..config import Config
from ..hubspot.client import HubSpotClient
from ..llm.base import LLM
from .world import World, step

OWNER_NAMES = {7100001 + i: n for i, n in enumerate(
    ["Maya Okafor", "Daniel Lindqvist", "Priya Ramos", "Owen Whitlock", "Lucia Haddad", "Marcus Brennan",
     "Hannah Castillo", "Theo Moreau"])}


def _u(*parts) -> float:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


def csm_decisions(conn: psycopg.Connection, cfg: Config, day: date) -> dict:
    approvals_began = cfg.launch_date + timedelta(days=cfg.shadow_days)
    weeks_in = max(0, (day - approvals_began).days) / 7
    counts = {"approve": 0, "edit": 0, "reject": 0}
    for r in conn.execute("SELECT * FROM ops.approvals WHERE status = 'open' ORDER BY id").fetchall():
        age = (day - r["requested_on"]).days
        if _u(r["id"], day, "when") > (0.5, 0.75, 1.0)[min(age, 2)]:
            continue
        roll, by = _u(r["id"], "decision"), OWNER_NAMES.get(r["owner_id"], "CSM")
        minutes, payload = None, None
        if r["action_type"] == "prepare_qbr":
            flagged = r["payload"].get("check_status") == "flagged"
            decision = "edit" if flagged or roll < 0.26 else ("reject" if roll > 0.985 else "approve")
            minutes = int(min(120, max(20, round(math.exp(math.log(45) + 0.3 * _normal(r["id"]))))))
        elif r["action_type"] == "change_deal_amount":
            decision = "reject" if roll < 0.1 else "approve"
        else:
            bad = 0.25 if weeks_in < 4 else (0.10 if weeks_in < 5 else 0.02)
            decision = "edit" if roll < bad * 0.75 else ("reject" if roll < bad else "approve")
        if decision == "edit" and "body" in r["payload"]:
            payload = {"body": r["payload"]["body"].replace("Happy to", "I'd be glad to")}
        approvals.decide(conn, r["id"], decision, by, day, payload, minutes)
        if minutes:
            conn.execute("INSERT INTO ops.time_logs (owner_id, company_id, activity, minutes, logged_on, source) "
                         "VALUES (%s, %s, 'qbr_prep', %s, %s, 'live')", (r["owner_id"], r["company_id"], minutes, day))
            conn.commit()
        counts[decision] += 1
    return counts


def _normal(seed) -> float:
    u1, u2 = max(_u(seed, "n1"), 1e-9), _u(seed, "n2")
    return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)


def flush_client_stats(conn: psycopg.Connection, crm: HubSpotClient, day: date) -> None:
    s = crm.take_stats()
    conn.execute(
        "INSERT INTO ops.client_stats VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (business_date) DO UPDATE SET "
        "requests = ops.client_stats.requests + EXCLUDED.requests, retries = ops.client_stats.retries + EXCLUDED.retries, "
        "rate_limited = ops.client_stats.rate_limited + EXCLUDED.rate_limited, "
        "server_errors = ops.client_stats.server_errors + EXCLUDED.server_errors, "
        "throttle_waits = ops.client_stats.throttle_waits + EXCLUDED.throttle_waits",
        (day, s.get("requests", 0), s.get("retries", 0), s.get("rate_limited", 0), s.get("server_errors", 0),
         s.get("throttle_waits", 0)))
    conn.commit()


def run_day(conn: psycopg.Connection, saver_conn: tuple, crm: HubSpotClient, portal: HubSpotClient,
            llm: LLM, cfg: Config, world: World, day: date, flush: Callable[[], dict], csms: bool = True) -> dict:
    db.set_today(conn, day)
    out = {"today": day.isoformat(), "world": step(conn, portal, world, day)}
    out["webhooks"] = flush()
    out["jobs"] = {}
    hooks = AgentContext(conn, crm, llm, cfg, day, saver_conn[0], source="webhook", approval_saver_conn=saver_conn[1])
    ctx = AgentContext(conn, crm, llm, cfg, day, saver_conn[0], source="nightly", approval_saver_conn=saver_conn[1])
    # In production the worker handles webhooks while the nightly run is in flight. The simulator gives the
    # day's webhooks to the worker after the health agent has planned and before it acts, which is the
    # ordering that once produced duplicate tasks.
    ctx.cache["before_act"] = lambda: out.__setitem__("jobs", jobs.process(hooks))
    out["agents"] = nightly.run_all(ctx)
    if csms:
        out["decisions"] = csm_decisions(conn, cfg, day)
    out["resumed"] = approvals.resume_decided(ctx)
    flush_client_stats(conn, crm, day)
    world.save(conn, day)
    return out


def run(conn, saver_conn, crm, portal, llm, cfg: Config, world: World, start: date, end: date,
        flush: Callable[[], dict], csms_until: date | None = None, progress: Callable[[dict], None] | None = None) -> list:
    errors = []
    day = start
    while day <= end:
        out = run_day(conn, saver_conn, crm, portal, llm, cfg, world, day, flush,
                      csms=csms_until is None or day <= csms_until)
        errors += [f"{day}: {a['agent']} {a['error']}" for a in out["agents"] if a["status"] != "done"]
        if progress:
            progress(out)
        day += timedelta(days=1)
    return errors
