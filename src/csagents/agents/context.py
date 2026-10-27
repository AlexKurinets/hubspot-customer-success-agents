"""What an agent run sees: one business date, one HubSpot client, one database connection, and per-run
caches so the three agents share the nightly reads instead of repeating them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import psycopg

from ..config import Config
from ..hubspot.client import HubSpotClient
from ..llm.base import LLM
from ..sources import billing
from ..sources.billing import BillingState

COMPANY_PROPS = ["name", "hubspot_owner_id", "cs_account_manager_id", "cs_arr", "cs_renewal_date", "cs_seats_licensed",
                 "cs_go_live_date", "cs_next_qbr_date", "notes_last_contacted", "cs_agent_health_score",
                 "cs_agent_health_tier"]
TICKET_PROPS = ["subject", "content", "hs_ticket_priority", "hs_pipeline_stage", "createdate", "closed_date"]

# Ticket-to-company associations never change once made, so they are cached for the life of the process.
_TICKET_COMPANY: dict[int, int] = {}


def _date(v: str | None) -> date | None:
    return date.fromisoformat(v[:10]) if v else None


def _int(v: str | None) -> int | None:
    return int(float(v)) if v not in (None, "") else None


def parse_company(obj: dict) -> dict:
    p = obj["properties"]
    return {
        "id": int(obj["id"]), "name": p.get("name") or "", "owner_id": _int(p.get("hubspot_owner_id")),
        "account_manager_id": _int(p.get("cs_account_manager_id")), "arr": float(p.get("cs_arr") or 0),
        "renewal_date": _date(p.get("cs_renewal_date")), "seats": _int(p.get("cs_seats_licensed")) or 0,
        "go_live": _date(p.get("cs_go_live_date")), "next_qbr": _date(p.get("cs_next_qbr_date")),
        "last_contacted": _date(p.get("notes_last_contacted")), "score": _int(p.get("cs_agent_health_score")),
        "tier": p.get("cs_agent_health_tier") or None,
    }


@dataclass
class AgentContext:
    conn: psycopg.Connection
    crm: HubSpotClient
    llm: LLM
    cfg: Config
    today: date
    saver_conn: psycopg.Connection | None = None
    source: str = "nightly"
    # Approval threads start inside a nightly graph's node, so they checkpoint on a connection of their own.
    approval_saver_conn: psycopg.Connection | None = None
    cache: dict = field(default_factory=dict)

    @property
    def shadow(self) -> bool:
        return self.today < self.cfg.launch_date + timedelta(days=self.cfg.shadow_days)

    def owners(self) -> dict[int, dict]:
        if "owners" not in self.cache:
            self.cache["owners"] = {int(o["id"]): o for o in self.crm.owners()}
        return self.cache["owners"]

    def owner_name(self, owner_id: int | None) -> str:
        o = self.owners().get(owner_id or 0)
        return f"{o['firstName']} {o['lastName']}" if o else "your customer success manager"

    def companies(self, ids: list[int]) -> dict[int, dict]:
        cached = self.cache.setdefault("companies", {})
        missing = [i for i in ids if i not in cached]
        for obj in self.crm.batch_read("companies", missing, COMPANY_PROPS):
            cached[int(obj["id"])] = parse_company(obj)
        return {i: cached[i] for i in ids if i in cached}

    def billing(self) -> dict[int, BillingState]:
        if "billing" not in self.cache:
            self.cache["billing"] = billing.states(self.conn, self.today)
        return self.cache["billing"]

    def tickets_since(self, since: date) -> dict[int, list[dict]]:
        """Tickets created on or after `since`, grouped by company."""
        key = ("tickets", since)
        if key not in self.cache:
            found = list(self.crm.search_all("tickets", [{"propertyName": "createdate", "operator": "GTE",
                                                           "value": since.isoformat()}], TICKET_PROPS))
            self.cache[key] = self.group_tickets(found)
        return self.cache[key]

    def group_tickets(self, tickets: list[dict]) -> dict[int, list[dict]]:
        unknown = [int(t["id"]) for t in tickets if int(t["id"]) not in _TICKET_COMPANY]
        for tid, companies in self.crm.associations("tickets", "companies", unknown).items():
            if companies:
                _TICKET_COMPANY[tid] = companies[0]
        out: dict[int, list[dict]] = {}
        for t in tickets:
            cid = _TICKET_COMPANY.get(int(t["id"]))
            if cid is not None:
                out.setdefault(cid, []).append({"id": int(t["id"]), **t["properties"]})
        return out
