"""The world around the agents: practices, their staff, product usage, tickets, email, billing, finance
and CSM activity. Every change to HubSpot goes through the API with the portal-users token, the way
people's edits reach the real CRM, so it raises webhooks exactly as theirs would.

Each account carries a hidden health value. Usage, adoption, tickets and failed payments are drawn from it
with the same functions that generated the renewal history the score was fitted on. A few accounts a
month start a decline episode with a reason (a departing practice manager, a price complaint, a competitor,
product trouble) that shows up in their tickets and emails.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import numpy as np
from psycopg.types.json import Jsonb

from ..hubspot.client import HubSpotClient, assoc
from ..sources.warehouse import week_start
from . import names

MODULE_OFFSETS = (1.0, 0.0, -0.8)
REASONS = ["product_issue", "pricing", "competitor", "champion_left"]
PRIORITIES = ["LOW", "MEDIUM", "HIGH"]
TICKET_TO_COMPANY, TICKET_TO_CONTACT, CONTACT_TO_COMPANY = 339, 16, 279
EMAIL_TO_COMPANY, EMAIL_TO_CONTACT = 186, 198
ROLES = ["practice_manager", "lead_veterinarian", "office_admin"]

SIM_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS sim;
CREATE TABLE IF NOT EXISTS sim.state (id int PRIMARY KEY CHECK (id = 1), accounts jsonb NOT NULL,
    open_tickets jsonb NOT NULL, failed_invoices jsonb NOT NULL, as_of date NOT NULL);
"""


def sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def utilization(h: float, offset: float, noise: float) -> float:
    return min(1.0, max(0.02, sig(0.6 * h + 0.2) + offset + noise))


def modules(h: float, thresholds: list[float]) -> list[bool]:
    return [t < sig(1.3 * h + off) for t, off in zip(thresholds, MODULE_OFFSETS, strict=True)]


def ticket_rate(h: float) -> float:
    return (0.5 + 6 * sig(-1.3 * h)) / 90


def invoice_fail_p(h: float) -> float:
    return 0.02 + 0.2 * sig(-1.5 * h - 0.5)


def stamp(day: date, hour: int = 14) -> str:
    return f"{day.isoformat()}T{hour:02d}:00:00.000Z"


@dataclass
class Account:
    id: int
    name: str
    h: float
    arr: float
    seats: int
    renewal: str
    go_live: str
    next_qbr: str | None
    owner: int
    am: int
    contacts: list[int]
    contact_names: list[str]
    offset: float
    thresholds: list[float]
    ramp_until: str | None = None
    drift: float = 0.0
    episode: str | None = None
    episode_until: str | None = None
    recover_until: str | None = None

    def live(self, day: date) -> bool:
        return date.fromisoformat(self.go_live) <= day


@dataclass
class World:
    accounts: dict[int, Account]
    open_tickets: list[dict] = field(default_factory=list)
    failed_invoices: list[dict] = field(default_factory=list)

    # ---------------------------------------------------------------- persistence

    def save(self, conn, as_of: date) -> None:
        conn.execute(SIM_SCHEMA)
        conn.execute(
            "INSERT INTO sim.state VALUES (1, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET accounts = EXCLUDED.accounts, "
            "open_tickets = EXCLUDED.open_tickets, failed_invoices = EXCLUDED.failed_invoices, as_of = EXCLUDED.as_of",
            (Jsonb([asdict(a) for a in self.accounts.values()]), Jsonb(self.open_tickets), Jsonb(self.failed_invoices),
             as_of))
        conn.commit()

    @classmethod
    def load(cls, conn) -> World:
        row = conn.execute("SELECT * FROM sim.state WHERE id = 1").fetchone()
        accounts = {a["id"]: Account(**a) for a in row["accounts"]}
        return cls(accounts, row["open_tickets"], row["failed_invoices"])


# ---------------------------------------------------------------- generation


def owners() -> list[dict]:
    out = [{"id": 7100001 + i, "email": f"{f}.{s}@example.com".lower(), "firstName": f, "lastName": s,
            "team": "Customer Success"} for i, (f, s) in enumerate(names.CSMS)]
    out += [{"id": 7100101 + i, "email": f"{f}.{s}@example.com".lower(), "firstName": f, "lastName": s,
             "team": "Account Management"} for i, (f, s) in enumerate(names.ACCOUNT_MANAGERS)]
    return out


def _slug(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())[:24]


def generate(conn, portal: HubSpotClient, launch: date, accounts: int = 1200, seed: int = 7) -> World:
    rng = np.random.default_rng(seed)
    portal.http.post("/_mock/owners", json={"owners": owners()})
    csm_ids = [7100001 + i for i in range(len(names.CSMS))]
    am_ids = [7100101 + i for i in range(len(names.ACCOUNT_MANAGERS))]
    n_new = round(accounts * 0.1)
    used: set[str] = set()
    specs, company_inputs = [], []
    for i in range(accounts):
        while True:
            name = f"{rng.choice(names.CLINIC_FIRST)} {rng.choice(names.CLINIC_SECOND)}"
            if name in used:
                name = f"{name} {rng.choice(names.TOWNS)}"
            if name not in used:
                used.add(name)
                break
        arr = float(min(240000, max(4000, round(math.exp(rng.normal(math.log(14000), 1.0)), -2))))
        seats = max(3, round(arr / 950))
        if i < n_new:
            go_live = launch + timedelta(days=int(rng.integers(-20, 130)))
            renewal = go_live + timedelta(days=365)
            h0, ramp = float(rng.normal(0.2, 0.4)), (go_live + timedelta(weeks=10)).isoformat()
        else:
            go_live = launch - timedelta(days=int(rng.integers(120, 1500)))
            renewal = launch + timedelta(days=int(rng.integers(1, 366)))
            h0, ramp = float(rng.normal(0.8, 1.0)), None
        next_qbr = (launch + timedelta(days=int(rng.integers(0, 91)))).isoformat() if arr >= 15000 else None
        owner, am = csm_ids[i % len(csm_ids)], am_ids[0 if arr < 20000 else 1]
        last_contact = launch - timedelta(days=int(rng.integers(0, 50)))
        specs.append(dict(name=name, h=h0, arr=arr, seats=seats, renewal=renewal.isoformat(), go_live=go_live.isoformat(),
                          next_qbr=next_qbr, owner=owner, am=am, offset=float(rng.normal(0, 0.1)),
                          thresholds=[float(x) for x in rng.random(3)], ramp_until=ramp))
        company_inputs.append({"properties": {
            "name": name, "domain": f"{_slug(name)}.example.com", "phone": f"555-01{i % 100:02d}",
            "city": str(rng.choice(names.TOWNS)), "hubspot_owner_id": str(owner), "cs_account_manager_id": str(am),
            "cs_arr": str(int(arr)), "cs_renewal_date": renewal.isoformat(), "cs_seats_licensed": str(seats),
            "cs_go_live_date": go_live.isoformat(), "cs_next_qbr_date": next_qbr or "",
            "notes_last_contacted": stamp(last_contact), "cs_segment": "multi-site" if arr >= 40000 else "single-site",
            "createdate": stamp(go_live - timedelta(days=30), 9)}})
    companies = portal.batch_create("companies", company_inputs)

    world = World({})
    contact_inputs, owners_of = [], []
    for spec, comp in zip(specs, companies, strict=True):
        cid = int(comp["id"])
        n = 1 + int(rng.random() < 0.8) + int(rng.random() < 0.5)
        for role in ROLES[:n]:
            f, s = str(rng.choice(names.FIRST)), str(rng.choice(names.LAST))
            contact_inputs.append({"properties": {
                "firstname": f, "lastname": s, "email": f"{f}.{s}@{_slug(spec['name'])}.example.com".lower(),
                "phone": f"555-01{int(rng.integers(0, 100)):02d}", "jobtitle": role.replace("_", " ").title(),
                "cs_role": role}, "associations": [assoc(cid, CONTACT_TO_COMPANY)]})
            owners_of.append((cid, f"{f} {s}"))
        world.accounts[cid] = Account(id=cid, contacts=[], contact_names=[], **spec)
    for (cid, full), obj in zip(owners_of, portal.batch_create("contacts", contact_inputs), strict=True):
        world.accounts[cid].contacts.append(int(obj["id"]))
        world.accounts[cid].contact_names.append(full)

    _history_usage(conn, world, launch, rng)
    _billing(conn, world, launch, rng)
    _history_tickets(portal, world, launch, rng)
    renewal_history(conn, launch, np.random.default_rng(seed + 1))
    qbr_time_history(conn, world, launch, rng)
    conn.commit()
    world.save(conn, launch - timedelta(days=1))
    return world


def _usage_row(a: Account, week: date, rng) -> tuple:
    util = utilization(a.h, a.offset, float(rng.normal(0, 0.14)))
    m = modules(a.h, a.thresholds)
    return (a.id, week, a.seats, max(0, round(a.seats * util)), *m)


def _copy_usage(conn, rows: list[tuple]) -> None:
    with conn.cursor().copy("COPY wh.usage_weekly (company_id, week_start, seats_licensed, weekly_active_users, "
                            "appointments_used, records_used, invoicing_used) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def _history_usage(conn, world: World, launch: date, rng) -> None:
    rows = []
    # Up to two weeks before launch; the launch-day Monday step writes the last week before launch.
    first = week_start(launch) - timedelta(weeks=30)
    for a in world.accounts.values():
        h_now = a.h
        for k in range(29):
            week = first + timedelta(weeks=k)
            if week < date.fromisoformat(a.go_live):
                continue
            a.h = h_now + float(rng.normal(0, 0.08))
            rows.append(_usage_row(a, week, rng))
        a.h = h_now
    _copy_usage(conn, rows)


def _billing(conn, world: World, launch: date, rng) -> None:
    for a in world.accounts.values():
        conn.execute("INSERT INTO billing.subscriptions VALUES (%s, %s, %s, %s, %s)",
                     (a.id, a.arr, a.seats, a.renewal, launch - timedelta(days=1)))
        for k in range(6, 0, -1):
            issued = date(launch.year, launch.month, 1) - timedelta(days=30 * k)
            issued = date(issued.year, issued.month, 1)
            if issued < date.fromisoformat(a.go_live):
                continue
            failed = rng.random() < invoice_fail_p(a.h) and k == 1
            conn.execute("INSERT INTO billing.invoices (company_id, issued_on, due_on, amount, status, paid_on) "
                         "VALUES (%s, %s, %s, %s, %s, %s)",
                         (a.id, issued, issued + timedelta(days=15), round(a.arr / 12, 2), "failed" if failed else "paid",
                          None if failed else issued + timedelta(days=int(rng.integers(0, 12)))))
            if failed:
                world.failed_invoices.append({"company_id": a.id, "resolve_on": (launch + timedelta(
                    days=int(rng.integers(3, 20)))).isoformat()})


def _ticket_input(a: Account, day: date, rng, cue: str | None) -> dict:
    subject, content = (names.TICKET_CUES[cue][int(rng.integers(0, len(names.TICKET_CUES[cue])))] if cue
                        else names.TICKET_NEUTRAL[int(rng.integers(0, len(names.TICKET_NEUTRAL)))])
    p_high = 0.2 + (0.15 if cue else 0)
    priority = str(rng.choice(PRIORITIES, p=[0.5 - p_high / 2, 0.5 - p_high / 2, p_high]))
    return {"properties": {"subject": subject, "content": content, "hs_pipeline": "0", "hs_pipeline_stage": "1",
                           "hs_ticket_priority": priority, "createdate": stamp(day, 9 + int(rng.integers(0, 8)))},
            "associations": [assoc(a.id, TICKET_TO_COMPANY), assoc(a.contacts[0], TICKET_TO_CONTACT)]}


def _history_tickets(portal: HubSpotClient, world: World, launch: date, rng) -> None:
    inputs, closes = [], []
    for a in world.accounts.values():
        if not a.live(launch):
            continue
        for _ in range(rng.poisson(ticket_rate(a.h) * 90)):
            day = launch - timedelta(days=int(rng.integers(1, 91)))
            inputs.append(_ticket_input(a, day, rng, None))
            closes.append(day + timedelta(days=int(rng.integers(1, 7))))
    created = portal.batch_create("tickets", inputs)
    updates = []
    for obj, close in zip(created, closes, strict=True):
        if close < launch:
            updates.append((int(obj["id"]), {"hs_pipeline_stage": "4", "closed_date": stamp(close, 16)}))
        else:
            world.open_tickets.append({"id": int(obj["id"]), "close_on": close.isoformat()})
    portal.batch_update("tickets", updates)


def renewal_history(conn, launch: date, rng) -> int:
    """Eighteen months of renewals before launch, from the same hidden-health model as the live accounts.
    Features are measured 90 days before each renewal. For renewals that were at risk, the day a CSM first
    touched them is recorded: that is the before figure for how early risk reached a CSM."""
    rows = []
    start = launch - timedelta(days=548)
    for _ in range(1800):
        h = float(rng.normal(0.8, 1.0))
        u = min(1.0, max(0.02, sig(0.6 * h + 0.2) + float(rng.normal(0, 0.15))))
        a = sum(float(rng.random()) < sig(1.3 * h + off) for off in MODULE_OFFSETS) / 3
        severity = sum(int(rng.choice([1, 2, 4], p=[0.5, 0.3, 0.2])) for _ in range(rng.poisson(0.5 + 6 * sig(-1.3 * h))))
        t = 1 - min(1.0, severity / 12)
        inv = 0.0 if rng.random() < 0.03 + 0.35 * sig(-1.5 * h - 0.5) else 1.0
        churned = bool(rng.random() < sig(-2.1 - 1.6 * h))
        at_risk = churned or h < 0.2
        touch = int(min(90, max(3, round(math.exp(rng.normal(math.log(25), 0.5)))))) if at_risk else None
        rows.append((start + timedelta(days=int(rng.integers(0, 548))), u, a, t, inv, churned, at_risk, touch))
    with conn.cursor().copy("COPY wh.renewal_history (renewal_date, seat_utilization, module_adoption, ticket_ok, "
                            "invoice_current, churned, at_risk, csm_first_touch_days) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    return len(rows)


def qbr_time_history(conn, world: World, launch: date, rng) -> None:
    """CSM-logged QBR prep time for the six months before launch, when packs were built by hand."""
    for a in world.accounts.values():
        if a.arr < 15000:
            continue
        for k in (1, 2):
            day = launch - timedelta(days=91 * k - int(rng.integers(0, 91)))
            minutes = int(min(330, max(90, round(math.exp(rng.normal(math.log(180), 0.25))))))
            conn.execute("INSERT INTO ops.time_logs (owner_id, company_id, activity, minutes, logged_on, source) "
                         "VALUES (%s, %s, 'qbr_prep', %s, %s, 'history')", (a.owner, a.id, minutes, day))


# ---------------------------------------------------------------- one day


def step(conn, portal: HubSpotClient, world: World, day: date, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed * 100_003 + day.toordinal())
    stats = {"tickets": 0, "emails": 0, "closed": 0}
    accts = [a for a in world.accounts.values()]
    if day.weekday() == 0:
        _weekly(conn, world, day, rng)

    tickets, emails = [], []
    for a in accts:
        if not a.live(day):
            continue
        for _ in range(rng.poisson(ticket_rate(a.h))):
            cue = a.episode if a.episode and rng.random() < 0.5 else None
            tickets.append(_ticket_input(a, day, rng, cue))
        text = None
        if a.h < -1.0 and rng.random() < 1 / 60:
            text = ("Contract", names.EMAIL_CANCEL)
        elif a.episode and rng.random() < 1 / 15:
            body = names.EMAIL_CUES[a.episode].format(name=a.contact_names[-1].split()[-1])
            text = ("Following up", body)
        elif rng.random() < 1 / 150:
            text = ("Question", str(rng.choice(names.EMAIL_NEUTRAL)))
        if text:
            emails.append(_email_input(a, day, rng, "INCOMING_EMAIL", *text))
        if rng.random() < 1 / 75:
            emails.append(_email_input(a, day, rng, "EMAIL", "Catching up", "Thanks for your time on the phone today."))
    for f in world.failed_invoices:
        a = world.accounts[f["company_id"]]
        if not f.get("emailed") and rng.random() < 0.15:
            emails.append(_email_input(a, day, rng, "INCOMING_EMAIL", "Invoice", names.EMAIL_BILLING))
            f["emailed"] = True
    if tickets:
        for obj in portal.batch_create("tickets", tickets):
            close = day + timedelta(days=int(rng.integers(1, 7)))
            world.open_tickets.append({"id": int(obj["id"]), "close_on": close.isoformat()})
        stats["tickets"] = len(tickets)
    if emails:
        portal.batch_create("emails", emails)
        stats["emails"] = len(emails)
    due = [t for t in world.open_tickets if t["close_on"] <= day.isoformat()]
    if due:
        portal.batch_update("tickets", [(t["id"], {"hs_pipeline_stage": "4", "closed_date": stamp(day, 16)}) for t in due])
        world.open_tickets = [t for t in world.open_tickets if t["close_on"] > day.isoformat()]
        stats["closed"] = len(due)

    stats.update(_billing_day(conn, world, day, rng))
    stats.update(_finance_and_csm_edits(conn, portal, world, day, rng))
    conn.commit()
    return stats


def _email_input(a: Account, day: date, rng, direction: str, subject: str, body: str) -> dict:
    k = int(rng.integers(0, len(a.contacts)))
    return {"properties": {"hs_timestamp": stamp(day, 10 + int(rng.integers(0, 7))), "hs_email_direction": direction,
                           "hs_email_status": "SENT", "hs_email_subject": subject, "hs_email_text": body,
                           "hubspot_owner_id": str(a.owner)},
            "associations": [assoc(a.contacts[k], EMAIL_TO_CONTACT), assoc(a.id, EMAIL_TO_COMPANY)]}


def _weekly(conn, world: World, day: date, rng) -> None:
    """Monday: health moves, episodes start and end, and last week's usage lands in the warehouse."""
    rows = []
    for a in world.accounts.values():
        today = day.isoformat()
        if a.ramp_until and today < a.ramp_until and a.live(day):
            a.h += 0.08
        if a.episode and today >= (a.episode_until or ""):
            a.episode, a.drift = None, 0.0
            if rng.random() < 0.5:
                a.recover_until, a.drift = (day + timedelta(weeks=6)).isoformat(), 0.07
        elif a.recover_until and today >= a.recover_until:
            a.recover_until, a.drift = None, 0.0
        if not a.episode and not a.recover_until and a.h > 0 and rng.random() < 0.012:
            a.episode = str(rng.choice(REASONS))
            a.episode_until = (day + timedelta(weeks=int(rng.integers(6, 15)))).isoformat()
            a.drift = -0.15
        a.h += a.drift + float(rng.normal(0, 0.07))
        week = day - timedelta(days=7)
        if a.live(week + timedelta(days=6)):
            rows.append(_usage_row(a, week, rng))
    _copy_usage(conn, rows)


def _billing_day(conn, world: World, day: date, rng) -> dict:
    out = {"invoices": 0, "disputes": 0}
    if day.day == 1:
        for a in world.accounts.values():
            if not a.live(day):
                continue
            failed = rng.random() < invoice_fail_p(a.h)
            conn.execute("INSERT INTO billing.invoices (company_id, issued_on, due_on, amount, status, paid_on) "
                         "VALUES (%s, %s, %s, %s, %s, %s)",
                         (a.id, day, day + timedelta(days=15), round(a.arr / 12, 2), "failed" if failed else "paid",
                          None if failed else day + timedelta(days=int(rng.integers(0, 12)))))
            if failed:
                world.failed_invoices.append({"company_id": a.id,
                                              "resolve_on": (day + timedelta(days=int(rng.integers(5, 26)))).isoformat()})
            out["invoices"] += 1
    for f in [f for f in world.failed_invoices if f["resolve_on"] <= day.isoformat()]:
        conn.execute("UPDATE billing.invoices SET status = 'paid', paid_on = %s WHERE company_id = %s AND status = 'failed'",
                     (day, f["company_id"]))
    world.failed_invoices = [f for f in world.failed_invoices if f["resolve_on"] > day.isoformat()]
    if rng.random() < 0.1:
        pool = [a for a in world.accounts.values() if a.live(day)]
        weights = np.array([sig(-a.h) for a in pool])
        a = pool[int(rng.choice(len(pool), p=weights / weights.sum()))]
        conn.execute("INSERT INTO billing.disputes (company_id, opened_on, closed_on, amount, reason) VALUES (%s, %s, %s, %s, %s)",
                     (a.id, day, day + timedelta(days=int(rng.integers(20, 46))), round(a.arr / 12, 2), "product_not_received"))
        out["disputes"] = 1
    return out


def _finance_and_csm_edits(conn, portal: HubSpotClient, world: World, day: date, rng) -> dict:
    out = {"renewal_amendments": 0, "seat_changes": 0, "deal_edits": 0}
    company_updates: dict[int, dict] = {}
    # Contract amendments: finance co-terms a contract and pulls its renewal date earlier.
    if rng.random() < 0.33:
        pool = [a for a in world.accounts.values()
                if 125 <= (date.fromisoformat(a.renewal) - day).days <= 200]
        if pool:
            a = pool[int(rng.integers(0, len(pool)))]
            a.renewal = (date.fromisoformat(a.renewal) - timedelta(days=int(rng.integers(10, 61)))).isoformat()
            company_updates.setdefault(a.id, {})["cs_renewal_date"] = a.renewal
            conn.execute("UPDATE billing.subscriptions SET current_period_end = %s, updated_on = %s WHERE company_id = %s",
                         (a.renewal, day, a.id))
            out["renewal_amendments"] = 1
    # Seat additions from busy practices.
    pool = [a for a in world.accounts.values() if a.live(day) and sig(0.6 * a.h + 0.2) + a.offset > 0.85]
    if pool and rng.random() < 0.9:
        a = pool[int(rng.integers(0, len(pool)))]
        added = int(rng.integers(1, 5))
        a.arr = float(round(a.arr / a.seats * (a.seats + added), -2))
        a.seats += added
        conn.execute("UPDATE billing.subscriptions SET seats = %s, arr = %s, updated_on = %s WHERE company_id = %s",
                     (a.seats, a.arr, day, a.id))
        company_updates.setdefault(a.id, {}).update({"cs_arr": str(int(a.arr)), "cs_seats_licensed": str(a.seats)})
        out["seat_changes"] = 1
    # Contracts that reached their date renew for another year.
    for a in world.accounts.values():
        if date.fromisoformat(a.renewal) <= day:
            a.renewal = (date.fromisoformat(a.renewal) + timedelta(days=365)).isoformat()
            company_updates.setdefault(a.id, {})["cs_renewal_date"] = a.renewal
            conn.execute("UPDATE billing.subscriptions SET current_period_end = %s WHERE company_id = %s", (a.renewal, a.id))
        if a.next_qbr and a.next_qbr < day.isoformat():
            a.next_qbr = (date.fromisoformat(a.next_qbr) + timedelta(days=91)).isoformat()
            company_updates.setdefault(a.id, {})["cs_next_qbr_date"] = a.next_qbr
    if company_updates:
        portal.batch_update("companies", list(company_updates.items()))
    # Finance sometimes sets a renewal amount by hand, to its own figure, while the agent's proposal waits.
    if rng.random() < 0.3:
        row = conn.execute("SELECT payload FROM ops.approvals WHERE action_type = 'change_deal_amount' AND status = 'open' "
                           "ORDER BY id LIMIT 1").fetchone()
        if row:
            portal.http.patch(f"/crm/v3/objects/deals/{row['payload']['deal_id']}",
                              json={"properties": {"amount": str(round(row["payload"]["new_amount"] * 0.97, -2))}},
                              headers={"Authorization": f"Bearer {portal.token}", "X-Mock-User": "7100201"})
            out["deal_edits"] = 1
    return out
