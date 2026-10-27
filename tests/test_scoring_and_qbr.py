"""The health score, the fit that sets its points, and the QBR figure check."""

from datetime import date, timedelta

import numpy as np
import pytest

from csagents import config, db, scoring
from csagents.agents.qbr import check
from csagents.llm.offline import OfflineLLM, _h
from csagents.sources.billing import BillingState
from csagents.sources.warehouse import UsageWeek, renewal_history
from csagents.synthetic import world

LAUNCH = date(2026, 2, 2)


def test_score_is_points_times_signals():
    cfg = config.get()
    weeks = [UsageWeek(date(2026, 1, 5) + timedelta(weeks=i), 20, 10, True, True, i == 3) for i in range(4)]
    feats = scoring.features(weeks, ["HIGH", "LOW", "MEDIUM"], BillingState(12000, 20, date(2026, 9, 1), True, False, False))
    assert feats == {"seat_utilization": 0.5, "module_adoption": 1.0, "ticket_ok": 0.4167, "invoice_current": 1.0}
    h = scoring.score(feats, cfg.weights, cfg.tier)
    expected = sum(cfg.weights[k] * feats[k] for k in scoring.FEATURES)
    assert h.score == round(expected)
    assert cfg.tier(60) == "green" and cfg.tier(59) == "yellow" and cfg.tier(45) == "yellow" and cfg.tier(44) == "red"


def test_points_always_sum_to_100():
    for coef in ([4.2, 1.3, 1.1, 0.4], [1, 1, 1, 1], [3.7, 0.9, 0.2, 0.6]):
        assert sum(scoring.to_points(np.array(coef)).values()) == 100


def test_fit_on_the_renewal_history_reproduces_the_committed_points():
    cfg = config.get()
    conn = db.connect()
    try:
        db.migrate(conn)
        conn.execute("TRUNCATE wh.renewal_history")
        world.renewal_history(conn, LAUNCH, np.random.default_rng(8))
        conn.commit()
        fit = scoring.fit_weights(renewal_history(conn), LAUNCH - timedelta(days=182))
    finally:
        conn.close()
    assert fit["points"] == cfg.weights
    assert 0.76 <= fit["held_out_auc"] <= 0.84
    assert fit["held_out_auc"] == pytest.approx(cfg.weights_meta["held_out_auc"])


FIGURES = {"active_users_avg": 41, "active_users_change_pct": -12, "seats": 52, "seat_utilization_pct": 79,
           "modules_adopted": 2, "tickets_opened": 6, "tickets_high": 1, "arr": 47300, "renewal_date": "2026-08-14",
           "days_to_renewal": 61}


def test_figure_check_passes_a_faithful_draft_and_catches_a_drifted_number():
    llm = OfflineLLM()
    ok = None
    for cid in range(1, 200):
        if _h(cid, "2026-06-10") % 8:
            ok = llm.write_qbr({"id": cid, "name": "Foxglen Pet Clinic", "meeting_date": "2026-06-10"}, FIGURES, 1, [])
            break
    assert ok and check(ok, FIGURES) == []
    assert "$47,300" in ok
    assert check(ok.replace("79%", "83%"), FIGURES) == ["83"]


def test_a_draft_that_misses_twice_is_flagged():
    llm = OfflineLLM()
    cid = next(i for i in range(1, 10_000) if _h(i, "2026-06-10") % 8 == 0 and _h(i, "2026-06-10") % 7 == 0)
    company = {"id": cid, "name": "Foxglen Pet Clinic", "meeting_date": "2026-06-10"}
    first = check(llm.write_qbr(company, FIGURES, 1, []), FIGURES)
    second = check(llm.write_qbr(company, FIGURES, 2, first), FIGURES)
    assert first == ["83"] and second == ["83"]
