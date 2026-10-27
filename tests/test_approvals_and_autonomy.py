"""Approval threads: wait in LangGraph, resume from another process, re-check the world before writing."""

from datetime import date

from conftest import LIVE, company

from csagents import approvals, autonomy, config, db
from csagents.actions import PlannedAction
from csagents.agents.dispatch import dispatch
from csagents.hubspot.client import DEAL_TO_COMPANY, assoc


def _contact(stack, cid):
    return stack.crm.associations("companies", "contacts", [cid])[cid][0]


def _checkin(cid, contact_id, arr=60000):
    return PlannedAction("send_checkin", "followup", cid, "Wrenhollow Animal Hospital", 7100001, "no_contact:2025-12-01",
                         {"contact_id": contact_id, "to_email": "rosa.quinlan@wrenhollow.example.com",
                          "subject": "Checking in", "body": "Hi Rosa, it has been a while.", "why": "no contact for 70 days"},
                         arr=arr)


def test_customer_facing_actions_wait_over_the_arr_threshold_or_with_billing_involved():
    cfg = config.get()
    a = _checkin(1, 2, arr=60000)
    assert autonomy.decide(cfg, a, {"send_checkin": True})[0] == "approve"
    a.arr = 9000
    assert autonomy.decide(cfg, a, {"send_checkin": True}) == ("act", "")
    a.billing_flag = True
    assert autonomy.decide(cfg, a, {"send_checkin": True})[0] == "approve"
    a.billing_flag = False
    assert autonomy.decide(cfg, a, {"send_checkin": False})[0] == "approve"
    never = PlannedAction("apply_discount", "followup", 1, "x", None, "t")
    assert autonomy.decide(cfg, never, {})[0] == "never"


def test_graduation_needs_fifty_decisions_with_under_five_percent_edited(stack):
    cfg = config.get()

    def decisions(edits):
        stack.conn.execute("DELETE FROM ops.approvals")
        for i in range(50):
            stack.conn.execute(
                "INSERT INTO ops.approvals (action_key, action_type, agent, company_id, company_name, reason, requested_on, "
                "payload, status, decision, decided_on) VALUES (%s, 'send_checkin', 'followup', 1, 'x', 'r', %s, '{}', "
                "'executed', %s, %s)", (f"k{i}", LIVE, "edit" if i < edits else "approve", LIVE))
        stack.conn.commit()
        return autonomy.graduation(stack.conn, cfg, "send_checkin")

    assert decisions(2).graduated
    assert not decisions(3).graduated
    stack.conn.execute("DELETE FROM ops.approvals WHERE action_key = 'k49'")
    assert not autonomy.graduation(stack.conn, cfg, "send_checkin").graduated  # 49 decisions is not enough


def test_an_approval_waits_and_resumes_in_another_process(stack):
    cid = company(stack, arr=60000)
    db.set_today(stack.conn, LIVE)
    stats = dispatch(stack.ctx(LIVE), [_checkin(cid, _contact(stack, cid))])
    assert stats["approval_requested"] == 1
    row = stack.conn.execute("SELECT * FROM ops.approvals").fetchone()
    assert row["status"] == "open" and "40,000" in row["reason"]
    assert stack.conn.execute("SELECT count(*) AS n FROM ops.outbox").fetchone()["n"] == 0

    assert approvals.decide(stack.conn, row["id"], "edit", "Maya Okafor", date(2026, 2, 11), {"body": "Hi Rosa, edited."})
    # A fresh context with new connections stands in for the worker process that picks the decision up.
    fresh = stack.ctx(date(2026, 2, 11), "approval")
    fresh.saver_conn = db.connect(autocommit=True)
    fresh.approval_saver_conn = db.connect(autocommit=True)
    assert approvals.resume_decided(fresh) == {"executed": 1}
    sent = stack.conn.execute("SELECT * FROM ops.outbox").fetchone()
    assert sent["body"] == "Hi Rosa, edited."
    ledger_row = stack.conn.execute("SELECT * FROM ops.ledger").fetchone()
    assert (ledger_row["autonomy"], ledger_row["status"]) == ("approve", "done")
    assert stack.conn.execute("SELECT outcome FROM ops.approvals").fetchone()["outcome"] == "approved by Maya Okafor"


def test_a_rejected_approval_writes_nothing(stack):
    cid = company(stack, arr=60000)
    db.set_today(stack.conn, LIVE)
    dispatch(stack.ctx(LIVE), [_checkin(cid, _contact(stack, cid))])
    approvals.decide(stack.conn, 1, "reject", "Maya Okafor", LIVE)
    assert approvals.resume_decided(stack.ctx(LIVE, "approval")) == {"rejected": 1}
    assert stack.conn.execute("SELECT count(*) AS n FROM ops.ledger").fetchone()["n"] == 0
    assert stack.conn.execute("SELECT count(*) AS n FROM ops.outbox").fetchone()["n"] == 0


def test_an_amount_change_is_cancelled_when_finance_edited_the_deal_after_the_request(stack):
    cid = company(stack)
    db.set_today(stack.conn, LIVE)
    deal = int(stack.crm.create("deals", {"dealname": "renewal", "amount": "12000"}, [assoc(cid, DEAL_TO_COMPANY)])["id"])
    a = PlannedAction("change_deal_amount", "health", cid, "Wrenhollow Animal Hospital", 7100001, "amount:2026-05-01:14000",
                      {"deal_id": deal, "old_amount": 12000, "new_amount": 14000, "why": "seats added"})
    assert dispatch(stack.ctx(LIVE), [a])["approval_requested"] == 1
    stack.portal.update("deals", deal, {"amount": "13500"})  # a person fixes it by hand
    approvals.decide(stack.conn, 1, "approve", "Maya Okafor", LIVE)
    assert approvals.resume_decided(stack.ctx(LIVE, "approval")) == {"cancelled": 1}
    outcome = stack.conn.execute("SELECT outcome FROM ops.approvals").fetchone()["outcome"]
    assert "edited by userId:1" in outcome
    assert stack.crm.get("deals", deal, ["amount"])["properties"]["amount"] == "13500"


def test_a_dispute_opened_while_waiting_cancels_the_email(stack):
    cid = company(stack, arr=60000)
    db.set_today(stack.conn, LIVE)
    stack.conn.execute("INSERT INTO billing.subscriptions VALUES (%s, 60000, 40, '2026-09-30', %s)", (cid, LIVE))
    stack.conn.commit()
    dispatch(stack.ctx(LIVE), [_checkin(cid, _contact(stack, cid))])
    stack.conn.execute("INSERT INTO billing.disputes (company_id, opened_on, amount, reason) VALUES (%s, %s, 5000, 'x')",
                       (cid, LIVE))
    stack.conn.commit()
    approvals.decide(stack.conn, 1, "approve", "Maya Okafor", LIVE)
    assert approvals.resume_decided(stack.ctx(LIVE, "approval")) == {"cancelled": 1}
    assert stack.conn.execute("SELECT count(*) AS n FROM ops.outbox").fetchone()["n"] == 0
    assert stack.conn.execute("SELECT precondition FROM ops.blocked_actions").fetchone()["precondition"] == "open_dispute"


def test_shadow_week_logs_without_writing(stack):
    cid = company(stack, arr=9000)
    a = _checkin(cid, _contact(stack, cid), arr=9000)
    stats = dispatch(stack.ctx(date(2026, 2, 4)), [a])
    assert stats["shadow"] == 1
    assert stack.conn.execute("SELECT would_need_approval FROM ops.shadow_log").fetchone()["would_need_approval"]
    assert stack.conn.execute("SELECT count(*) AS n FROM ops.ledger").fetchone()["n"] == 0
