"""A stand-in for the part of HubSpot's CRM API the agents call: v3 objects, batch and search, v4
associations, the properties and owners APIs, and propertyChange webhooks signed with the v3 scheme.

It keeps HubSpot's behaviour where the agents depend on it: every value is a string, writes to a
property that doesn't exist fail, batches stop at 100 inputs, search pages stop at 200 results and at
10,000 results per query, every response carries the rate-limit headers, and going over the burst
limit returns a 429. Changes made by the agents' private app arrive in webhooks as INTEGRATION changes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib import resources

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

SINGULAR = {"companies": "company", "contacts": "contact", "deals": "deal", "tickets": "ticket",
            "tasks": "task", "notes": "note", "emails": "email"}

# HubSpot-defined association type ids for the pairs this stack uses.
ASSOCIATION_TYPES = {
    1: ("contacts", "companies"), 279: ("contacts", "companies"),
    5: ("deals", "companies"), 341: ("deals", "companies"),
    26: ("tickets", "companies"), 339: ("tickets", "companies"), 16: ("tickets", "contacts"),
    192: ("tasks", "companies"), 204: ("tasks", "contacts"), 216: ("tasks", "deals"),
    190: ("notes", "companies"), 202: ("notes", "contacts"), 214: ("notes", "deals"),
    186: ("emails", "companies"), 198: ("emails", "contacts"),
}
DEFAULT_ASSOCIATION = {(279, "contacts", "companies"), (341, "deals", "companies"), (339, "tickets", "companies"),
                       (16, "tickets", "contacts"), (192, "tasks", "companies"), (204, "tasks", "contacts"),
                       (190, "notes", "companies"), (202, "notes", "contacts"), (186, "emails", "companies"),
                       (198, "emails", "contacts"), (216, "tasks", "deals"), (214, "notes", "deals")}

# The portal's own properties: HubSpot defaults plus the customer-success fields the client's admin
# maintains. The agents create their own cs_agent_ group through the properties API.
PORTAL_PROPERTIES = {
    "companies": [("name", "string"), ("domain", "string"), ("phone", "string"), ("city", "string"),
                  ("hubspot_owner_id", "enumeration"), ("notes_last_contacted", "datetime"),
                  ("cs_arr", "number"), ("cs_renewal_date", "date"), ("cs_seats_licensed", "number"),
                  ("cs_go_live_date", "date"), ("cs_next_qbr_date", "date"),
                  ("cs_account_manager_id", "enumeration"), ("cs_segment", "string")],
    "contacts": [("firstname", "string"), ("lastname", "string"), ("email", "string"), ("phone", "string"),
                 ("jobtitle", "string"), ("cs_role", "string")],
    "deals": [("dealname", "string"), ("amount", "number"), ("closedate", "date"), ("dealstage", "enumeration"),
              ("pipeline", "enumeration"), ("hubspot_owner_id", "enumeration")],
    "tickets": [("subject", "string"), ("content", "string"), ("hs_pipeline", "enumeration"),
                ("hs_pipeline_stage", "enumeration"), ("hs_ticket_priority", "enumeration"),
                ("closed_date", "datetime"), ("hubspot_owner_id", "enumeration")],
    "tasks": [("hs_task_subject", "string"), ("hs_task_body", "string"), ("hs_timestamp", "datetime"),
              ("hs_task_status", "enumeration"), ("hs_task_priority", "enumeration"), ("hs_task_type", "enumeration"),
              ("hubspot_owner_id", "enumeration")],
    "notes": [("hs_note_body", "string"), ("hs_timestamp", "datetime"), ("hubspot_owner_id", "enumeration")],
    "emails": [("hs_timestamp", "datetime"), ("hs_email_direction", "enumeration"), ("hs_email_status", "enumeration"),
               ("hs_email_subject", "string"), ("hs_email_text", "string"), ("hubspot_owner_id", "enumeration")],
}
READ_ONLY = {"hs_object_id", "hs_lastmodifieddate"}
DEFAULT_READ = {
    "companies": ["name", "domain"], "contacts": ["firstname", "lastname", "email"],
    "deals": ["dealname", "amount", "closedate", "dealstage", "pipeline"],
    "tickets": ["subject", "content", "hs_pipeline_stage", "hs_ticket_priority"],
    "tasks": ["hs_task_subject", "hs_task_status", "hs_timestamp"], "notes": ["hs_note_body", "hs_timestamp"],
    "emails": ["hs_email_subject", "hs_timestamp"],
}
OPERATORS = {"EQ": "=", "NEQ": "<>", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}
GRANTS_ALL = {t: "rw" for t in [*SINGULAR, "owners", "schemas"]}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _error(status: int, message: str, category: str, **extra) -> JSONResponse:
    return JSONResponse({"status": "error", "message": message, "category": category, **extra}, status_code=status)


def sign_v3(secret: str, method: str, uri: str, body: str, timestamp: str) -> str:
    digest = hmac.new(secret.encode(), f"{method}{uri}{body}{timestamp}".encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class Limiter:
    """A rolling ten-second window per token, like HubSpot's TEN_SECONDLY_ROLLING policy."""

    def __init__(self, limit: int, window_s: float = 10.0):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def hit(self, token: str) -> tuple[bool, int]:
        with self.lock:
            now, q = time.monotonic(), self.hits[token]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False, 0
            q.append(now)
            return True, self.limit - len(q)


class Settings:
    def __init__(self, env: dict | None = None):
        e = {**os.environ, **(env or {})}
        self.dsn = e.get("DATABASE_URL", "postgresql://cs:cs@localhost:5490/cs")
        self.app_token = e.get("HUBSPOT_TOKEN", "pat-demo-cs-agents")
        self.app_id = e.get("HUBSPOT_APP_ID", "4100217")
        self.portal_token = e.get("MOCK_PORTAL_TOKEN", "pat-demo-portal-users")
        self.app_grants = dict(g.split(":") for g in e.get(
            "MOCK_APP_GRANTS",
            "companies:rw,contacts:r,deals:rw,tickets:r,tasks:rw,notes:rw,emails:rw,owners:r,schemas:rw").split(","))
        self.burst_limit = int(e.get("MOCK_BURST_LIMIT", "100"))
        self.burst_window_ms = int(e.get("MOCK_BURST_WINDOW_MS", "10000"))
        self.search_cap = int(e.get("MOCK_SEARCH_CAP", "10000"))
        self.batch_max = 100
        self.page_max = 200
        self.secret = e.get("HUBSPOT_CLIENT_SECRET", "demo-client-secret")
        self.webhook_target = e.get("MOCK_WEBHOOK_TARGET", "")
        self.webhook_interval = float(e.get("MOCK_WEBHOOK_INTERVAL", "0"))
        self.duplicate_percent = int(e.get("MOCK_WEBHOOK_DUPLICATE_PERCENT", "0"))
        subs = e.get("MOCK_WEBHOOK_SUBSCRIPTIONS",
                     "company.propertyChange:cs_renewal_date,deal.propertyChange:amount,deal.propertyChange:dealstage")
        self.subscriptions: dict[tuple[str, str], int] = {}
        for i, s in enumerate(filter(None, subs.split(","))):
            kind, prop = s.split(":")
            self.subscriptions[(kind, prop)] = 1800000 + i


class Store:
    def __init__(self, dsn: str):
        self.pool = ConnectionPool(dsn, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=True)
        self.types: dict[str, dict[str, str]] = {}

    def migrate(self) -> None:
        sql = resources.files("hubspot_mock").joinpath("schema.sql").read_text(encoding="utf-8")
        with self.pool.connection() as c:
            c.execute(sql)
            for object_type, props in PORTAL_PROPERTIES.items():
                group = "customer_success" if object_type == "companies" else f"{SINGULAR[object_type]}information"
                c.execute("INSERT INTO hs.property_groups VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                          (object_type, group, group.replace("_", " ").title()))
                for name, ptype in props:
                    c.execute("INSERT INTO hs.properties VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                              (object_type, name, name, ptype, "text", group))
        self.load_types()

    def load_types(self) -> None:
        types: dict[str, dict[str, str]] = defaultdict(dict)
        with self.pool.connection() as c:
            for r in c.execute("SELECT object_type, name, type FROM hs.properties"):
                types[r["object_type"]][r["name"]] = r["type"]
        self.types = dict(types)


def create_app(env: dict | None = None, deliver: Callable[[str, dict, str], int] | None = None) -> FastAPI:
    cfg = Settings(env)
    store = Store(cfg.dsn)
    limiter = Limiter(cfg.burst_limit, cfg.burst_window_ms / 1000)
    faults: list[dict] = []
    stats = defaultdict(int)
    deliver_lock = threading.Lock()
    auto = {"on": True}
    stop = threading.Event()

    def default_deliver(url: str, headers: dict, body: str) -> int:
        try:
            return httpx.post(url, content=body, headers=headers, timeout=10).status_code
        except httpx.HTTPError:
            return 599

    send = deliver or default_deliver

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.migrate()
        thread = None
        if cfg.webhook_interval > 0 and cfg.webhook_target:
            def loop():
                while not stop.wait(cfg.webhook_interval):
                    if auto["on"]:
                        deliver_pending()
            thread = threading.Thread(target=loop, daemon=True)
            thread.start()
        yield
        stop.set()
        store.pool.close()

    app = FastAPI(title="HubSpot CRM stand-in", lifespan=lifespan)
    app.state.store = store

    # ---------------------------------------------------------------- auth, rate limit, faults

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if not request.url.path.startswith("/crm/"):
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token == cfg.app_token:
            request.state.grants = cfg.app_grants
            request.state.source = ("INTEGRATION", cfg.app_id)
        elif token == cfg.portal_token:
            request.state.grants = GRANTS_ALL
            request.state.source = ("CRM_UI", f"userId:{request.headers.get('x-mock-user', '1')}")
        else:
            return _error(401, "Authentication credentials not found.", "INVALID_AUTHENTICATION")
        stats["requests"] += 1
        allowed, remaining = limiter.hit(token)
        headers = {"X-HubSpot-RateLimit-Max": str(cfg.burst_limit), "X-HubSpot-RateLimit-Remaining": str(remaining),
                   "X-HubSpot-RateLimit-Interval-Milliseconds": str(cfg.burst_window_ms)}
        if not allowed:
            stats["rate_limited"] += 1
            resp = _error(429, "You have reached your ten_secondly_rolling limit.", "RATE_LIMITS",
                          errorType="RATE_LIMIT", policyName="TEN_SECONDLY_ROLLING")
            resp.headers.update(headers)
            return resp
        for f in faults:
            if f["count"] > 0 and f["match"] in request.url.path and request.method in f["methods"]:
                f["count"] -= 1
                stats["faults"] += 1
                resp = _error(f["status"], "Injected fault", "INTERNAL_ERROR")
                resp.headers.update(headers)
                return resp
        response = await call_next(request)
        response.headers.update(headers)
        return response

    def need(request: Request, object_type: str, access: str):
        if access not in request.state.grants.get(object_type, ""):
            return _error(403, "This app hasn't been granted all required scopes to make this call.",
                          "MISSING_SCOPES", context={"requiredGranularScopes": [f"{object_type}:{access}"]})
        return None

    def known(object_type: str):
        if object_type not in SINGULAR:
            return _error(400, f"Unable to infer object type from: {object_type}", "VALIDATION_ERROR")
        return None

    # ---------------------------------------------------------------- object helpers

    def validate(object_type: str, props: dict, creating: bool) -> JSONResponse | None:
        types = store.types.get(object_type, {})
        bad = []
        for name in props:
            if name in READ_ONLY or (name == "createdate" and not creating):
                bad.append({"isValid": False, "message": f"{name} is a read only property", "error": "READ_ONLY_VALUE",
                            "name": name})
            elif name not in types and name != "createdate":
                bad.append({"isValid": False, "message": f'Property "{name}" does not exist',
                            "error": "PROPERTY_DOESNT_EXIST", "name": name})
        if bad:
            return _error(400, f"Property values were not valid: {json.dumps(bad)}", "VALIDATION_ERROR")
        return None

    def shape(row: dict, object_type: str, wanted: list[str] | None, with_history: list[str] | None = None) -> dict:
        p = row["properties"]
        names = wanted if wanted else DEFAULT_READ.get(object_type, [])
        out = {n: p.get(n) for n in names if n in p or n in store.types.get(object_type, {})}
        out.update({"hs_object_id": str(row["id"]), "createdate": p.get("createdate"),
                    "hs_lastmodifieddate": row["updated_at"].isoformat(timespec="milliseconds").replace("+00:00", "Z")})
        obj = {"id": str(row["id"]), "properties": out,
               "createdAt": row["created_at"].isoformat(timespec="milliseconds").replace("+00:00", "Z"),
               "updatedAt": row["updated_at"].isoformat(timespec="milliseconds").replace("+00:00", "Z"),
               "archived": False}
        if with_history:
            obj["propertiesWithHistory"] = {n: row["history"].get(n, []) for n in with_history}
        return obj

    def history_entries(props: dict, source: tuple[str, str]) -> dict:
        ts = _now_iso()
        return {k: {"value": v, "timestamp": ts, "sourceType": source[0], "sourceId": source[1]} for k, v in props.items()}

    def clean(props: dict) -> dict:
        return {k: ("" if v is None else str(v)) for k, v in props.items()}

    def insert(c, object_type: str, props: dict, assocs: list, source) -> dict:
        props = clean(props)
        props.setdefault("createdate", _now_iso())
        hist = {k: [e] for k, e in history_entries(props, source).items()}
        row = c.execute(
            "INSERT INTO hs.objects (object_type, id, properties, history) "
            "VALUES (%s, nextval('hs.object_id_seq'), %s, %s) RETURNING *",
            (object_type, Jsonb(props), Jsonb(hist))).fetchone()
        for a in assocs or []:
            to_id = int(a["to"]["id"])
            for t in a["types"]:
                pair = ASSOCIATION_TYPES.get(int(t["associationTypeId"]))
                if pair is None or pair[0] != object_type:
                    raise ValueError(f"association type {t['associationTypeId']} is not valid from {object_type}")
                c.execute("INSERT INTO hs.associations VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                          (object_type, row["id"], pair[1], to_id, int(t["associationTypeId"])))
                if object_type == "emails" and pair[1] == "companies" and props.get("hs_timestamp"):
                    c.execute(
                        "UPDATE hs.objects SET properties = properties || jsonb_build_object('notes_last_contacted', %s::text) "
                        "WHERE object_type = 'companies' AND id = %s "
                        "AND COALESCE(properties->>'notes_last_contacted', '') < %s",
                        (props["hs_timestamp"], to_id, props["hs_timestamp"]))
        return row

    def update(c, object_type: str, object_id: int, props: dict, source) -> dict | None:
        rows = update_many(c, object_type, [(object_id, props)], source)
        return rows[0][0] if rows else None

    def update_many(c, object_type: str, items: list[tuple[int, dict]], source) -> list[tuple[dict, dict]]:
        """Locks and reads the rows in one query, writes the changed ones in one batch, queues webhooks."""
        found = {r["id"]: r for r in c.execute(
            "SELECT * FROM hs.objects WHERE object_type = %s AND id = ANY(%s) ORDER BY id FOR UPDATE",
            (object_type, [i for i, _ in items])).fetchall()}
        kind = f"{SINGULAR[object_type]}.propertyChange"
        out, writes, events = [], [], []
        for object_id, props in items:
            row = found.get(object_id)
            if row is None:
                continue
            props = clean(props)
            changed = {k: v for k, v in props.items() if row["properties"].get(k) != v}
            if changed:
                hist = row["history"]
                for k, e in history_entries(changed, source).items():
                    hist[k] = [e, *hist.get(k, [])][:20]
                row["properties"] = {**row["properties"], **changed}
                row["updated_at"] = datetime.now(UTC)
                writes.append((Jsonb(row["properties"]), Jsonb(hist), object_type, object_id))
                for k, v in changed.items():
                    sub = cfg.subscriptions.get((kind, k))
                    if sub is not None:
                        events.append({"subscriptionId": sub, "portalId": 24100001, "appId": int(cfg.app_id),
                                       "occurredAt": int(time.time() * 1000), "subscriptionType": kind,
                                       "attemptNumber": 0, "objectId": object_id, "propertyName": k,
                                       "propertyValue": v, "changeSource": source[0], "sourceId": source[1]})
            out.append((row, props))
        if writes:
            c.cursor().executemany("UPDATE hs.objects SET properties = %s, history = %s, updated_at = now() "
                                   "WHERE object_type = %s AND id = %s", writes)
        for e in events:
            e["eventId"] = c.execute("SELECT nextval('hs.event_id_seq') AS n").fetchone()["n"]
            c.execute("INSERT INTO hs.webhook_outbox (event) VALUES (%s)", (Jsonb(e),))
        return out

    # ---------------------------------------------------------------- objects API

    @app.post("/crm/v3/objects/{object_type}")
    def create_object(object_type: str, body: dict, request: Request):
        if (e := known(object_type) or need(request, object_type, "w")):
            return e
        if (e := validate(object_type, body.get("properties", {}), True)):
            return e
        with store.pool.connection() as c:
            try:
                row = insert(c, object_type, body.get("properties", {}), body.get("associations", []), request.state.source)
            except ValueError as ex:
                c.rollback()
                return _error(400, str(ex), "VALIDATION_ERROR")
        return JSONResponse(shape(row, object_type, list(body.get("properties", {}))), status_code=201)

    @app.get("/crm/v3/objects/{object_type}/{object_id}")
    def get_object(object_type: str, object_id: int, request: Request, properties: str = "",
                   propertiesWithHistory: str = ""):
        if (e := known(object_type) or need(request, object_type, "r")):
            return e
        with store.pool.connection() as c:
            row = c.execute("SELECT * FROM hs.objects WHERE object_type = %s AND id = %s", (object_type, object_id)).fetchone()
        if row is None:
            return _error(404, "Object not found", "OBJECT_NOT_FOUND")
        wanted = [p for p in properties.split(",") if p]
        hist = [p for p in propertiesWithHistory.split(",") if p]
        return shape(row, object_type, wanted, hist)

    @app.patch("/crm/v3/objects/{object_type}/{object_id}")
    def patch_object(object_type: str, object_id: int, body: dict, request: Request):
        if (e := known(object_type) or need(request, object_type, "w")):
            return e
        if (e := validate(object_type, body.get("properties", {}), False)):
            return e
        with store.pool.connection() as c:
            row = update(c, object_type, object_id, body.get("properties", {}), request.state.source)
        if row is None:
            return _error(404, "Object not found", "OBJECT_NOT_FOUND")
        return shape(row, object_type, list(body.get("properties", {})))

    def too_many(inputs: list):
        if len(inputs) > cfg.batch_max:
            return _error(400, f"Too many inputs: {len(inputs)}, the limit is {cfg.batch_max}", "VALIDATION_ERROR")
        return None

    @app.post("/crm/v3/objects/{object_type}/batch/read")
    def batch_read(object_type: str, body: dict, request: Request):
        inputs = body.get("inputs", [])
        if (e := known(object_type) or need(request, object_type, "r") or too_many(inputs)):
            return e
        ids = [int(i["id"]) for i in inputs]
        with store.pool.connection() as c:
            rows = c.execute("SELECT * FROM hs.objects WHERE object_type = %s AND id = ANY(%s) ORDER BY id",
                             (object_type, ids)).fetchall()
        return {"status": "COMPLETE", "results": [
            shape(r, object_type, body.get("properties"), body.get("propertiesWithHistory")) for r in rows]}

    @app.post("/crm/v3/objects/{object_type}/batch/create")
    def batch_create(object_type: str, body: dict, request: Request):
        inputs = body.get("inputs", [])
        if (e := known(object_type) or need(request, object_type, "w") or too_many(inputs)):
            return e
        for i in inputs:
            if (e := validate(object_type, i.get("properties", {}), True)):
                return e
        with store.pool.connection() as c:
            try:
                rows = [insert(c, object_type, i.get("properties", {}), i.get("associations", []), request.state.source)
                        for i in inputs]
            except ValueError as ex:
                c.rollback()
                return _error(400, str(ex), "VALIDATION_ERROR")
        return JSONResponse({"status": "COMPLETE", "results": [shape(r, object_type, None) for r in rows]},
                            status_code=201)

    @app.post("/crm/v3/objects/{object_type}/batch/update")
    def batch_update(object_type: str, body: dict, request: Request):
        inputs = body.get("inputs", [])
        if (e := known(object_type) or need(request, object_type, "w") or too_many(inputs)):
            return e
        for i in inputs:
            if (e := validate(object_type, i.get("properties", {}), False)):
                return e
        with store.pool.connection() as c:
            rows = update_many(c, object_type, [(int(i["id"]), i.get("properties", {})) for i in inputs],
                               request.state.source)
        return {"status": "COMPLETE", "results": [shape(r, object_type, list(p)) for r, p in rows]}

    @app.post("/crm/v3/objects/{object_type}/search")
    def search(object_type: str, body: dict, request: Request):
        if (e := known(object_type) or need(request, object_type, "r")):
            return e
        limit = int(body.get("limit", 10))
        after = int(body.get("after") or 0)
        if limit > cfg.page_max:
            return _error(400, f"limit must be at most {cfg.page_max}", "VALIDATION_ERROR")
        if after + limit > cfg.search_cap:
            return _error(400, f"Search results are limited to {cfg.search_cap} per query; narrow the filters.",
                          "VALIDATION_ERROR")
        types = store.types.get(object_type, {})
        groups, params = [], [object_type]
        for g in body.get("filterGroups", []):
            clauses = []
            for f in g.get("filters", []):
                sql, p = filter_sql(f, types)
                clauses.append(sql)
                params.extend(p)
            if clauses:
                groups.append("(" + " AND ".join(clauses) + ")")
        where = "object_type = %s" + (" AND (" + " OR ".join(groups) + ")" if groups else "")
        order = []
        for s in body.get("sorts", []) or ["hs_object_id"]:
            name = s if isinstance(s, str) else s["propertyName"]
            if not re.fullmatch(r"[a-z0-9_]+", name):
                return _error(400, f"Invalid sort property {name}", "VALIDATION_ERROR")
            direction = "DESC" if isinstance(s, dict) and s.get("direction") == "DESCENDING" else "ASC"
            if name == "hs_object_id":
                order.append(f"id {direction}")
            elif types.get(name) == "number":
                order.append(f"NULLIF(properties->>'{name}', '')::numeric {direction} NULLS LAST")
            else:
                order.append(f"properties->>'{name}' {direction} NULLS LAST")
        with store.pool.connection() as c:
            rows = c.execute(f"SELECT *, count(*) OVER () AS total FROM hs.objects WHERE {where} "
                             f"ORDER BY {', '.join(order)}, id LIMIT %s OFFSET %s", [*params, limit, after]).fetchall()
            total = rows[0]["total"] if rows else c.execute(
                f"SELECT count(*) AS n FROM hs.objects WHERE {where}", params).fetchone()["n"]
        out = {"total": total, "results": [shape(r, object_type, body.get("properties")) for r in rows]}
        if after + limit < total:
            out["paging"] = {"next": {"after": str(after + limit)}}
        return out

    def filter_sql(f: dict, types: dict) -> tuple[str, list]:
        name, op = f["propertyName"], f["operator"]
        numeric = name == "hs_object_id" or types.get(name) == "number"
        col = "id" if name == "hs_object_id" else (
            "NULLIF(properties->>%s, '')::numeric" if numeric else "properties->>%s")
        col_params = [] if name == "hs_object_id" else [name]

        def val(v):
            return float(v) if numeric else str(v)

        if op == "EQ" and not numeric:
            return "properties @> %s", [Jsonb({name: str(f["value"])})]
        if op in OPERATORS:
            return f"{col} {OPERATORS[op]} %s", [*col_params, val(f["value"])]
        if op == "IN":
            return f"{col} = ANY(%s)", [*col_params, [val(v) for v in f["values"]]]
        if op == "BETWEEN":
            return f"{col} BETWEEN %s AND %s", [*col_params, val(f["value"]), val(f["highValue"])]
        if op == "HAS_PROPERTY":
            return "COALESCE(properties->>%s, '') <> ''", [name]
        if op == "NOT_HAS_PROPERTY":
            return "COALESCE(properties->>%s, '') = ''", [name]
        raise ValueError(f"unsupported operator {op}")

    # ---------------------------------------------------------------- v4 associations

    @app.post("/crm/v4/associations/{from_type}/{to_type}/batch/read")
    def assoc_read(from_type: str, to_type: str, body: dict, request: Request):
        inputs = body.get("inputs", [])
        if (e := known(from_type) or known(to_type) or need(request, from_type, "r") or need(request, to_type, "r")):
            return e
        if len(inputs) > 1000:
            return _error(400, "Too many inputs", "VALIDATION_ERROR")
        ids = [int(i["id"]) for i in inputs]
        found: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
        with store.pool.connection() as c:
            for r in c.execute("SELECT from_id, to_id, type_id FROM hs.associations "
                               "WHERE from_type = %s AND to_type = %s AND from_id = ANY(%s)", (from_type, to_type, ids)):
                found[r["from_id"]][r["to_id"]].append(r["type_id"])
            for r in c.execute("SELECT to_id AS from_id, from_id AS to_id, type_id FROM hs.associations "
                               "WHERE to_type = %s AND from_type = %s AND to_id = ANY(%s)", (from_type, to_type, ids)):
                found[r["from_id"]][r["to_id"]].append(r["type_id"])
        results = [{"from": {"id": str(i)}, "to": [
            {"toObjectId": t, "associationTypes": [{"category": "HUBSPOT_DEFINED", "typeId": x, "label": None} for x in tids]}
            for t, tids in sorted(found[i].items())]} for i in ids if i in found]
        return {"status": "COMPLETE", "results": results}

    @app.put("/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}")
    def assoc_default(from_type: str, from_id: int, to_type: str, to_id: int, request: Request):
        if (e := known(from_type) or known(to_type) or need(request, from_type, "w")):
            return e
        tid = next((t for t, f, to in DEFAULT_ASSOCIATION if f == from_type and to == to_type), None)
        if tid is None:
            return _error(400, f"No default association from {from_type} to {to_type}", "VALIDATION_ERROR")
        with store.pool.connection() as c:
            c.execute("INSERT INTO hs.associations VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                      (from_type, from_id, to_type, to_id, tid))
        return {"status": "COMPLETE", "results": [{"from": {"id": str(from_id)}, "to": {"id": str(to_id)},
                                                    "associationSpec": {"associationCategory": "HUBSPOT_DEFINED",
                                                                        "associationTypeId": tid}}]}

    # ---------------------------------------------------------------- properties and owners

    @app.get("/crm/v3/properties/{object_type}/groups/{name}")
    def get_group(object_type: str, name: str, request: Request):
        if (e := need(request, "schemas", "r")):
            return e
        with store.pool.connection() as c:
            row = c.execute("SELECT * FROM hs.property_groups WHERE object_type = %s AND name = %s",
                            (object_type, name)).fetchone()
        return {"name": row["name"], "label": row["label"]} if row else _error(404, "group not found", "OBJECT_NOT_FOUND")

    @app.post("/crm/v3/properties/{object_type}/groups")
    def create_group(object_type: str, body: dict, request: Request):
        if (e := need(request, "schemas", "w")):
            return e
        with store.pool.connection() as c:
            n = c.execute("INSERT INTO hs.property_groups VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                          (object_type, body["name"], body["label"])).rowcount
        if not n:
            return _error(409, f"A group named {body['name']} already exists", "OBJECT_ALREADY_EXISTS")
        return JSONResponse({"name": body["name"], "label": body["label"]}, status_code=201)

    @app.get("/crm/v3/properties/{object_type}/{name}")
    def get_property(object_type: str, name: str, request: Request):
        if (e := need(request, "schemas", "r")):
            return e
        with store.pool.connection() as c:
            row = c.execute("SELECT * FROM hs.properties WHERE object_type = %s AND name = %s", (object_type, name)).fetchone()
        if row is None:
            return _error(404, f"Unable to find property {name}", "OBJECT_NOT_FOUND")
        return {"name": row["name"], "label": row["label"], "type": row["type"], "fieldType": row["field_type"],
                "groupName": row["group_name"]}

    @app.post("/crm/v3/properties/{object_type}")
    def create_property(object_type: str, body: dict, request: Request):
        if (e := need(request, "schemas", "w")):
            return e
        with store.pool.connection() as c:
            if not c.execute("SELECT 1 FROM hs.property_groups WHERE object_type = %s AND name = %s",
                             (object_type, body["groupName"])).fetchone():
                return _error(400, f"Property group {body['groupName']} does not exist", "VALIDATION_ERROR")
            n = c.execute("INSERT INTO hs.properties VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                          (object_type, body["name"], body["label"], body["type"], body["fieldType"],
                           body["groupName"])).rowcount
        if not n:
            return _error(409, f"Property {body['name']} already exists", "OBJECT_ALREADY_EXISTS")
        store.load_types()
        return JSONResponse(body, status_code=201)

    @app.get("/crm/v3/owners")
    def owners(request: Request):
        if (e := need(request, "owners", "r")):
            return e
        with store.pool.connection() as c:
            rows = c.execute("SELECT * FROM hs.owners ORDER BY id").fetchall()
        return {"results": [{"id": str(r["id"]), "email": r["email"], "firstName": r["first_name"],
                             "lastName": r["last_name"], "teams": [{"name": r["team"]}]} for r in rows]}

    # ---------------------------------------------------------------- webhooks

    def deliver_pending(limit: int = 5000) -> dict:
        if not cfg.webhook_target:
            return {"delivered": 0, "failed": 0, "duplicates_sent": 0}
        delivered = failed = dupes = 0
        with deliver_lock, store.pool.connection() as c:
            rows = c.execute("SELECT id, event, attempts FROM hs.webhook_outbox WHERE delivered_at IS NULL "
                             "ORDER BY id LIMIT %s", (limit,)).fetchall()
            for start in range(0, len(rows), 100):
                chunk = rows[start:start + 100]
                events = [{**r["event"], "attemptNumber": r["attempts"]} for r in chunk]
                status = post_events(events)
                ids = [r["id"] for r in chunk]
                if 200 <= status < 300:
                    delivered += len(chunk)
                    c.execute("UPDATE hs.webhook_outbox SET delivered_at = now(), attempts = attempts + 1 WHERE id = ANY(%s)",
                              (ids,))
                    # At-least-once delivery: HubSpot occasionally sends an event again.
                    again = [e for e in events if int(e["eventId"]) % 100 < cfg.duplicate_percent]
                    if again and 200 <= post_events(again) < 300:
                        dupes += len(again)
                else:
                    failed += len(chunk)
                    c.execute("UPDATE hs.webhook_outbox SET attempts = attempts + 1, last_error = %s WHERE id = ANY(%s)",
                              (f"HTTP {status}", ids))
        return {"delivered": delivered, "failed": failed, "duplicates_sent": dupes}

    def post_events(events: list[dict]) -> int:
        body = json.dumps(events, separators=(",", ":"))
        ts = str(int(time.time() * 1000))
        headers = {"Content-Type": "application/json", "X-HubSpot-Request-Timestamp": ts,
                   "X-HubSpot-Signature-v3": sign_v3(cfg.secret, "POST", cfg.webhook_target, body, ts),
                   "X-HubSpot-Signature-Version": "v3"}
        return send(cfg.webhook_target, headers, body)

    app.state.deliver_pending = deliver_pending

    # ---------------------------------------------------------------- mock controls (not HubSpot)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/_mock/webhooks/flush")
    def flush():
        return deliver_pending()

    @app.post("/_mock/webhooks/auto")
    def auto_delivery(body: dict):
        """Pauses timed delivery so a backfill can deliver each simulated day's webhooks itself, in order."""
        auto["on"] = bool(body.get("enabled", True))
        return auto

    @app.post("/_mock/faults")
    def add_fault(body: dict):
        faults.append({"status": int(body.get("status", 503)), "count": int(body.get("count", 1)),
                       "match": body.get("match", "/crm/"), "methods": body.get("methods", ["GET", "POST", "PATCH", "PUT"])})
        return {"faults": len(faults)}

    @app.delete("/_mock/faults")
    def clear_faults():
        faults.clear()
        return {"faults": 0}

    @app.get("/_mock/stats")
    def get_stats():
        with store.pool.connection() as c:
            pending = c.execute("SELECT count(*) AS n FROM hs.webhook_outbox WHERE delivered_at IS NULL").fetchone()["n"]
        return {**stats, "webhooks_pending": pending, "burst_limit": cfg.burst_limit}

    @app.post("/_mock/reset")
    def reset():
        """Empties the portal: objects, associations, owners, queued webhooks and app-created properties."""
        with store.pool.connection() as c:
            c.execute("TRUNCATE hs.objects, hs.associations, hs.webhook_outbox, hs.owners")
            c.execute("DELETE FROM hs.properties WHERE group_name = 'cs_agent'")
            c.execute("DELETE FROM hs.property_groups WHERE name = 'cs_agent'")
            c.execute("ALTER SEQUENCE hs.object_id_seq RESTART")
            c.execute("ALTER SEQUENCE hs.event_id_seq RESTART")
        store.migrate()
        limiter.hits.clear()
        faults.clear()
        stats.clear()
        return {"reset": True}

    @app.post("/_mock/owners")
    def add_owners(body: dict):
        with store.pool.connection() as c:
            for o in body["owners"]:
                c.execute("INSERT INTO hs.owners VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                          (o["id"], o["email"], o["firstName"], o["lastName"], o["team"]))
        return {"owners": len(body["owners"])}

    return app
