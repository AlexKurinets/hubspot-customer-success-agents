"""Idempotency end to end: webhook verification and redelivery, the ledger, replay after a crash or an outage."""

import json
import time

from conftest import LIVE, Stack, company

from csagents import db, ledger, replay
from csagents.actions import PlannedAction
from csagents.agents import jobs, nightly
from csagents.executor import Executor, WriteNotAllowed, guard
from csagents.hubspot import webhooks


def _usage(stack, cid):
    stack.conn.execute("INSERT INTO wh.usage_weekly VALUES (%s, '2026-02-02', 12, 9, true, true, false)", (cid,))
    stack.conn.commit()


def _keyed(stack, object_type, key):
    return stack.crm.search(object_type, [{"propertyName": "cs_agent_action_key", "operator": "EQ", "value": key}],
                            ["cs_agent_action_key"])["results"]


def _task(cid, trigger="tier_drop:red:2026-02-09"):
    return PlannedAction("open_recovery_task", "health", cid, "Wrenhollow Animal Hospital", 7100001, trigger,
                         {"subject": "Health dropped to red", "body": "Call the practice manager.", "due_in_days": 2,
                          "why": "Health dropped to red (44)."})


def test_signature_v3_verification():
    body = json.dumps([{"eventId": 1}])
    now = int(time.time() * 1000)
    sig = webhooks.signature("s3cret", "POST", "https://api.example.com/webhooks/hubspot", body, str(now))
    assert webhooks.verify("s3cret", "POST", "https://api.example.com/webhooks/hubspot", body, str(now), sig) is None
    assert webhooks.verify("s3cret", "POST", "https://api.example.com/webhooks/hubspot", body + " ", str(now), sig) \
        == "signature mismatch"
    old = str(now - 6 * 60 * 1000)
    sig_old = webhooks.signature("s3cret", "POST", "https://api.example.com/webhooks/hubspot", body, old)
    assert "five minutes" in webhooks.verify("s3cret", "POST", "https://api.example.com/webhooks/hubspot", body, old, sig_old)


def test_receiver_rejects_unsigned_requests(stack):
    r = stack.api.post("/webhooks/hubspot", content="[]", headers={"X-HubSpot-Signature-v3": "x",
                                                                   "X-HubSpot-Request-Timestamp": str(int(time.time() * 1000))})
    assert r.status_code == 401
    assert stack.conn.execute("SELECT count(*) AS n FROM ops.webhook_rejections").fetchone()["n"] == 1


def test_redelivered_events_are_stored_once_and_own_changes_are_not_acted_on():
    s = Stack(mock_env={"MOCK_WEBHOOK_DUPLICATE_PERCENT": "100"})
    try:
        cid = company(s)
        s.portal.update("companies", cid, {"cs_renewal_date": "2026-05-01"})
        deal = s.crm.create("deals", {"dealname": "x", "amount": "100"}, [])
        s.crm.update("deals", int(deal["id"]), {"amount": "200"})
        out = s.flush()
        assert out["delivered"] == 2 and out["duplicates_sent"] == 2
        rows = {r["property_name"]: r["status"] for r in s.conn.execute("SELECT * FROM ops.webhook_events")}
        assert rows == {"cs_renewal_date": "queued", "amount": "own_change"}
        assert s.conn.execute("SELECT count(*) AS n FROM ops.webhook_duplicates").fetchone()["n"] == 2
        assert s.conn.execute("SELECT count(*) AS n FROM ops.jobs").fetchone()["n"] == 1
    finally:
        s.close()


def test_webhook_and_nightly_run_reaching_one_change_open_one_deal(stack):
    """The production bug: keyed on the webhook event id, both paths created a task. Keyed on the business
    event, the second claim collides and is recorded instead of written."""
    cid = company(stack, renewal="2026-09-30")
    _usage(stack, cid)
    db.set_today(stack.conn, LIVE)
    stack.portal.update("companies", cid, {"cs_renewal_date": "2026-05-01"})  # finance co-terms the contract
    stack.flush()
    nightly_ctx = stack.ctx(LIVE, "nightly")
    # The webhook is handled while the nightly health run sits between planning and acting.
    nightly_ctx.cache["before_act"] = lambda: jobs.process(stack.ctx(LIVE, "webhook"))
    out = nightly.run_agent(nightly_ctx, "health")
    assert out["status"] == "done" and out["dispatch_duplicate"] == 1
    key = f"create_renewal_deal:{cid}:renewal:2026-05-01"
    assert len(_keyed(stack, "deals", key)) == 1
    collisions = stack.conn.execute("SELECT source FROM ops.ledger_collisions WHERE action_key = %s", (key,)).fetchall()
    assert [c["source"] for c in collisions] == ["nightly"]
    assert stack.conn.execute("SELECT source FROM ops.ledger WHERE action_key = %s", (key,)).fetchone()["source"] == "webhook"


def test_the_same_action_twice_writes_once(stack):
    cid = company(stack)
    a = _task(cid)
    assert Executor(stack.conn, stack.crm, LIVE, "nightly").run(a, "act") == "done"
    assert Executor(stack.conn, stack.crm, LIVE, "webhook").run(a, "act") == "duplicate"
    assert len(_keyed(stack, "tasks", a.key)) == 1
    note = _keyed(stack, "notes", a.key)
    assert len(note) == 1


def test_agents_cannot_write_fields_people_own_without_approval(stack):
    cid = company(stack)
    try:
        guard({"amount": "1"})
        raise AssertionError("guard let a human-owned field through")
    except WriteNotAllowed:
        pass
    deal = stack.crm.create("deals", {"dealname": "renewal", "amount": "12000"}, [])
    a = PlannedAction("change_deal_amount", "health", cid, "Wrenhollow Animal Hospital", 7100001, "amount:x",
                      {"deal_id": int(deal["id"]), "old_amount": 12000, "new_amount": 14000, "why": "seats added"})
    assert Executor(stack.conn, stack.crm, LIVE, "nightly").run(a, "act") == "failed"
    assert "human-owned" in stack.conn.execute("SELECT error FROM ops.ledger").fetchone()["error"]
    assert stack.crm.get("deals", int(deal["id"]), ["amount"])["properties"]["amount"] == "12000"


def test_a_claim_left_by_a_crash_is_reconciled_from_hubspot(stack):
    """The write landed but the process died before marking the row done: replay finds the record by its
    action key and marks it done instead of writing it again."""
    cid = company(stack)
    a = _task(cid)
    assert ledger.claim(stack.conn, a, "act", LIVE, "nightly")
    Executor(stack.conn, stack.crm, LIVE, "nightly").task(a)  # the HubSpot write, then the crash
    stack.conn.execute("UPDATE ops.ledger SET claimed_at = now() - interval '1 hour'")
    stack.conn.commit()
    assert replay.stale_claims(stack.ctx(LIVE, "replay")) == {"found_in_hubspot": 1, "performed": 0}
    assert len(_keyed(stack, "tasks", a.key)) == 1
    assert stack.conn.execute("SELECT status FROM ops.ledger").fetchone()["status"] == "done"


def test_an_outage_goes_to_the_dead_letter_queue_and_replays(stack):
    cid = company(stack)
    a = _task(cid)
    stack.mock.post("/_mock/faults", json={"status": 503, "count": 5, "match": "/objects/tasks"})
    assert Executor(stack.conn, stack.crm, LIVE, "nightly").run(a, "act") == "dead"
    assert stack.conn.execute("SELECT count(*) AS n FROM ops.dead_letters").fetchone()["n"] == 1
    out = replay.dead_letters(stack.ctx(LIVE, "replay"))
    assert out["actions_done"] == 1
    assert len(_keyed(stack, "tasks", a.key)) == 1
    assert stack.conn.execute("SELECT count(*) AS n FROM ops.dead_letters WHERE replayed_at IS NULL").fetchone()["n"] == 0


def test_a_failed_nightly_run_resumes_from_its_checkpoint(stack):
    cid = company(stack, renewal="2026-05-01")
    _usage(stack, cid)
    db.set_today(stack.conn, LIVE)
    stack.mock.post("/_mock/faults", json={"status": 503, "count": 5, "match": "/companies/batch/read"})
    first = nightly.run_agent(stack.ctx(LIVE), "health")
    assert first["status"] == "failed"
    second = nightly.run_agent(stack.ctx(LIVE), "health", resume=True)
    assert second["status"] == "done"
    assert stack.conn.execute("SELECT status FROM ops.runs WHERE thread_id = %s",
                              (f"health:{LIVE}",)).fetchone()["status"] == "done"
    assert len(_keyed(stack, "deals", f"create_renewal_deal:{cid}:renewal:2026-05-01")) == 1
