"""HTTP surface: the HubSpot webhook receiver, the approval endpoint the Slack buttons called, and the
read endpoints the dashboard uses. The API never writes to HubSpot itself; approving records the decision
and the worker resumes the waiting LangGraph thread."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Header, HTTPException, Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from . import approvals, config, db, metrics, settings
from .agents.jobs import enqueue
from .hubspot import webhooks
from .hubspot.client import HubSpotClient, HubSpotError

TIMELINE_PROPS = {
    "notes": ["hs_note_body", "hs_timestamp"],
    "tasks": ["hs_task_subject", "hs_task_body", "hs_timestamp", "hs_task_status"],
    "emails": ["hs_email_subject", "hs_email_text", "hs_email_direction", "hs_timestamp"],
    "deals": ["dealname", "amount", "closedate", "cs_agent_risk_tier"],
}


class Decision(BaseModel):
    decision: str
    by: str
    body: str | None = None
    review_minutes: int | None = None


def create_app(database_url: str | None = None, crm: HubSpotClient | None = None) -> FastAPI:
    s = settings.load()
    pool = ConnectionPool(database_url or s.database_url, kwargs={"row_factory": dict_row}, min_size=1, max_size=6,
                          open=False)
    names: dict = {"at": 0.0, "by_id": {}}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open()
        with pool.connection() as c:
            db.migrate(c)
        yield
        pool.close()

    app = FastAPI(title="Customer Success agents", lifespan=lifespan)
    app.state.crm = crm or HubSpotClient()

    def company_names() -> dict[int, dict]:
        if time.monotonic() - names["at"] > 600 or not names["by_id"]:
            names["by_id"] = {int(o["id"]): o["properties"] for o in app.state.crm.search_all(
                "companies", [], ["name", "hubspot_owner_id", "cs_arr", "cs_renewal_date"])}
            names["at"] = time.monotonic()
        return names["by_id"]

    @app.get("/health")
    def health():
        return {"ok": True}

    # ---------------------------------------------------------------- webhooks

    @app.post("/webhooks/hubspot")
    async def hubspot_webhook(request: Request):
        body = (await request.body()).decode()
        reason = webhooks.verify(s.hubspot_client_secret, "POST", str(request.url), body,
                                 request.headers.get("x-hubspot-request-timestamp"),
                                 request.headers.get("x-hubspot-signature-v3"))
        with pool.connection() as c:
            if reason:
                c.execute("INSERT INTO ops.webhook_rejections (reason) VALUES (%s)", (reason,))
                c.commit()
                raise HTTPException(401, reason)
            counts = {"queued": 0, "duplicate": 0, "own_change": 0}
            for e in json.loads(body):
                own = e.get("changeSource") == "INTEGRATION" and str(e.get("sourceId")) == s.hubspot_app_id
                status = "own_change" if own else "queued"
                row = c.execute(
                    "INSERT INTO ops.webhook_events (event_id, subscription_type, object_id, property_name, property_value, "
                    "change_source, source_id, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                    (e["eventId"], e["subscriptionType"], e["objectId"], e.get("propertyName"), e.get("propertyValue"),
                     e.get("changeSource"), str(e.get("sourceId")), status)).fetchone()
                if row is None:
                    c.execute("INSERT INTO ops.webhook_duplicates (event_id) VALUES (%s)", (e["eventId"],))
                    counts["duplicate"] += 1
                    continue
                if not own:
                    enqueue(c, f"{e['subscriptionType']}:{e.get('propertyName')}", e)
                counts[status] += 1
        return counts

    # ---------------------------------------------------------------- approvals

    @app.get("/approvals")
    def list_approvals(status: str = "open", limit: int = 100):
        with pool.connection() as c:
            if status == "all":
                rows = c.execute("SELECT * FROM ops.approvals ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM ops.approvals WHERE status = %s ORDER BY id LIMIT %s",
                                 (status, limit)).fetchall()
        return rows

    @app.get("/approvals/{approval_id}")
    def get_approval(approval_id: int):
        with pool.connection() as c:
            row = c.execute("SELECT * FROM ops.approvals WHERE id = %s", (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such approval")
        return row

    @app.post("/approvals/{approval_id}/decide")
    def decide(approval_id: int, d: Decision, x_approver_token: str | None = Header(default=None)):
        if x_approver_token != s.approver_token:
            raise HTTPException(401, "missing or wrong X-Approver-Token")
        if d.decision not in ("approve", "edit", "reject"):
            raise HTTPException(422, "decision must be approve, edit or reject")
        with pool.connection() as c:
            payload = {"body": d.body} if d.decision == "edit" and d.body else None
            if not approvals.decide(c, approval_id, d.decision, d.by, db.today(c), payload, d.review_minutes):
                raise HTTPException(409, "approval is not open")
        return {"id": approval_id, "decision": d.decision, "note": "the worker carries this out within a few seconds"}

    # ---------------------------------------------------------------- metrics and accounts

    @app.get("/metrics/summary")
    def summary():
        with pool.connection() as c:
            out = metrics.summary(c, config.get())
        out["csms"] = sum(1 for o in app.state.crm.owners() if o["teams"][0]["name"] == "Customer Success")
        return out

    @app.get("/metrics/monthly")
    def monthly():
        with pool.connection() as c:
            return metrics.monthly(c)

    @app.get("/metrics/reliability")
    def reliability():
        with pool.connection() as c:
            return metrics.reliability(c)

    @app.get("/accounts")
    def accounts(tier: str | None = None, limit: int = 2000):
        with pool.connection() as c:
            rows = c.execute(
                "SELECT * FROM (SELECT DISTINCT ON (company_id) company_id, business_date, score, tier, reasons "
                "FROM ops.health_scores ORDER BY company_id, business_date DESC) t "
                + ("WHERE tier = %s " if tier else "") + "ORDER BY score, company_id LIMIT %s",
                ([tier] if tier else []) + [limit]).fetchall()
        n = company_names()
        return [{**r, "name": n.get(r["company_id"], {}).get("name"),
                 "renewal_date": n.get(r["company_id"], {}).get("cs_renewal_date"),
                 "arr": float(n.get(r["company_id"], {}).get("cs_arr") or 0)} for r in rows]

    @app.get("/accounts/{company_id}")
    def account(company_id: int):
        crm = app.state.crm
        try:
            company = crm.get("companies", company_id, ["name", "hubspot_owner_id", "cs_arr", "cs_renewal_date",
                                                        "cs_seats_licensed", "cs_next_qbr_date", "notes_last_contacted",
                                                        "cs_agent_health_score", "cs_agent_health_tier",
                                                        "cs_agent_health_reason", "cs_agent_reviewed_at"])
        except HubSpotError as e:
            raise HTTPException(503 if e.status >= 500 else e.status, f"HubSpot: {e.message}") from e
        timeline, timeline_error = [], None
        try:
            for object_type, props in TIMELINE_PROPS.items():
                ids = crm.associations("companies", object_type, [company_id]).get(company_id, [])
                for o in crm.batch_read(object_type, ids, props):
                    p = o["properties"]
                    timeline.append({"type": object_type, "id": int(o["id"]),
                                     "when": (p.get("hs_timestamp") or p.get("closedate") or "")[:10], **p})
        except HubSpotError as e:
            timeline_error = f"HubSpot timeline unavailable: {e.message}"
        timeline.sort(key=lambda x: x["when"], reverse=True)
        with pool.connection() as c:
            history = c.execute("SELECT business_date, score, tier, points, reasons FROM ops.health_scores "
                                "WHERE company_id = %s ORDER BY business_date", (company_id,)).fetchall()
            ledger = c.execute("SELECT action_key, action_type, autonomy, status, business_date, source, payload "
                               "FROM ops.ledger WHERE company_id = %s ORDER BY business_date DESC, id DESC",
                               (company_id,)).fetchall()
            approval_rows = c.execute("SELECT id, action_type, status, decision, decided_by, outcome, requested_on "
                                      "FROM ops.approvals WHERE company_id = %s ORDER BY id DESC", (company_id,)).fetchall()
            qbr = c.execute("SELECT id, meeting_date, check_status, attempts, mismatches, figures, narrative "
                            "FROM ops.qbr_drafts WHERE company_id = %s ORDER BY id DESC", (company_id,)).fetchall()
        return {"company": company["properties"], "health_history": history, "ledger": ledger,
                "approvals": approval_rows, "qbr_drafts": qbr, "timeline": timeline[:40], "timeline_error": timeline_error}

    @app.get("/qbr/{draft_id}")
    def qbr(draft_id: int):
        with pool.connection() as c:
            row = c.execute("SELECT * FROM ops.qbr_drafts WHERE id = %s", (draft_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such draft")
        return row

    @app.get("/business-date")
    def business_date() -> dict[str, date]:
        with pool.connection() as c:
            return {"today": db.today(c)}

    return app
