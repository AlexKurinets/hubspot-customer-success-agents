"""Eight weeks of a small portal: the three agents, webhooks, simulated CSMs and the API together."""

from datetime import date

import pytest


@pytest.fixture(autouse=True)
def _release(seeded):
    """The seeded portal outlives this module; end each test's read transaction so later resets aren't blocked."""
    yield
    seeded["stack"].conn.rollback()


def test_eight_weeks_run_without_errors(seeded):
    assert seeded["errors"] == []
    conn = seeded["stack"].conn
    assert conn.execute("SELECT count(*) AS n FROM ops.runs WHERE status <> 'done'").fetchone()["n"] == 0


def test_nothing_was_written_during_the_shadow_week(seeded):
    conn = seeded["stack"].conn
    first = conn.execute("SELECT min(business_date) AS d FROM ops.ledger").fetchone()["d"]
    assert first >= date(2026, 2, 9)
    assert conn.execute("SELECT max(business_date) AS d FROM ops.shadow_log").fetchone()["d"] < date(2026, 2, 9)


def test_no_action_reached_hubspot_twice(seeded):
    conn = seeded["stack"].conn
    dupes = conn.execute(
        "SELECT properties->>'cs_agent_action_key' AS k, count(*) AS n FROM hs.objects "
        "WHERE object_type <> 'notes' AND COALESCE(properties->>'cs_agent_action_key', '') <> '' "
        "GROUP BY 1 HAVING count(*) > 1").fetchall()
    assert dupes == []
    done = conn.execute("SELECT count(*) AS n FROM ops.ledger WHERE status = 'done'").fetchone()["n"]
    stamped = conn.execute("SELECT count(DISTINCT properties->>'cs_agent_action_key') AS n FROM hs.objects "
                           "WHERE object_type = 'notes' AND COALESCE(properties->>'cs_agent_action_key', '') <> ''"
                           ).fetchone()["n"]
    assert stamped == done  # every completed action left exactly one explanatory note


def test_every_agent_did_work_and_people_were_asked(seeded):
    conn = seeded["stack"].conn
    types = {r["action_type"] for r in conn.execute("SELECT DISTINCT action_type FROM ops.ledger WHERE status = 'done'")}
    assert {"create_renewal_deal", "update_renewal_risk", "open_recovery_task", "send_checkin", "prepare_qbr"} <= types
    statuses = {r["status"] for r in conn.execute("SELECT DISTINCT status FROM ops.approvals")}
    assert "executed" in statuses and "open" in statuses


def test_the_api_serves_the_dashboard_figures(seeded):
    api = seeded["stack"].api
    s = api.get("/metrics/summary").json()
    assert s["today"] == "2026-03-31" and s["csms"] == 8
    assert s["accounts"] == sum(s["tiers"].values()) > 100
    assert 0 < s["actions"]["autonomous_share"] < 1
    assert api.get("/metrics/reliability").status_code == 200
    worst = api.get("/accounts", params={"tier": "red"}).json()
    assert worst and api.get(f"/accounts/{worst[0]['company_id']}").json()["company"]["name"]


def test_deciding_needs_the_approver_token_and_an_open_approval(seeded):
    api = seeded["stack"].api
    open_ones = api.get("/approvals").json()
    assert open_ones
    r = api.post(f"/approvals/{open_ones[0]['id']}/decide", json={"decision": "approve", "by": "Test"})
    assert r.status_code == 401
    done = api.get("/approvals", params={"status": "executed"}).json()[0]
    r = api.post(f"/approvals/{done['id']}/decide", json={"decision": "approve", "by": "Test"},
                 headers={"X-Approver-Token": "demo-csm"})
    assert r.status_code == 409
