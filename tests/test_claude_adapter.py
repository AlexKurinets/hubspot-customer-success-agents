"""The Claude adapter against a stub client: the tool loop, argument validation, refusals, structured QBR output."""

from types import SimpleNamespace

import pytest

from csagents.llm.base import LLMError, QbrNarrative
from csagents.llm.claude import ClaudeLLM

CONTEXT = {"company": {"id": 7, "name": "Foxglen Pet Clinic"}, "trigger": {"kind": "usage_drop"},
           "contacts": [{"id": 11, "name": "Rosa Quinlan"}], "recent_text": [{"source": "email", "text": "price increase"}]}


def _use(i, name, args):
    return SimpleNamespace(type="tool_use", id=f"tu_{i}", name=name, input=args)


class Stub:
    def __init__(self, turns, parsed=None):
        self.turns, self.calls = list(turns), []
        self.parsed = parsed
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create, parse=self._parse))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.turns.pop(0)

    def _parse(self, **kw):
        self.calls.append(kw)
        return self.parsed


def test_review_runs_the_tool_loop_and_collects_proposals_only():
    stub = Stub([
        SimpleNamespace(stop_reason="tool_use", content=[_use(1, "get_account_context", {})]),
        SimpleNamespace(stop_reason="tool_use", content=[
            _use(2, "record_reasons", {"reasons": [{"code": "pricing", "evidence": "price increase"}],
                                       "cancellation_language": False, "billing_language": False}),
            _use(3, "send_checkin", {"kind": "checkin", "contact_id": 11, "subject": "Checking in", "body": "Hi Rosa"}),
            _use(4, "create_task", {"subject": "x", "body": "y", "due_in_days": 3}),
            _use(5, "send_checkin", {"kind": "checkin", "contact_id": "eleven"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")]),
    ])
    review = ClaudeLLM(client=stub, model="claude-sonnet-5-5", effort="low").review_account(
        CONTEXT, ["send_checkin", "escalate_to_csm"])
    assert [r.code for r in review.reasons] == ["pricing"]
    assert [c.name for c in review.tool_calls] == ["send_checkin"]
    first = stub.calls[0]
    assert first["model"] == "claude-sonnet-5-5" and first["tool_choice"] == {"type": "auto"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert {t["name"] for t in first["tools"]} == {"get_account_context", "record_reasons", "send_checkin", "escalate_to_csm"}
    results = stub.calls[2]["messages"][-1]["content"]
    assert [r["is_error"] for r in results] == [False, False, True, True]  # create_task not allowed; bad contact_id


def test_a_refusal_raises_instead_of_guessing():
    stub = Stub([SimpleNamespace(stop_reason="refusal", content=[])])
    with pytest.raises(LLMError):
        ClaudeLLM(client=stub, model="m", effort="low").review_account(CONTEXT, ["send_checkin"])


def test_qbr_uses_structured_output_and_passes_back_the_bad_numbers():
    stub = Stub([], parsed=SimpleNamespace(stop_reason="end_turn", parsed_output=QbrNarrative(text="Average 41 users.")))
    text = ClaudeLLM(client=stub, model="m", effort="low").write_qbr({"id": 7, "name": "Foxglen"}, {"active_users_avg": 41},
                                                                     2, ["83"])
    assert text == "Average 41 users."
    assert stub.calls[0]["output_format"] is QbrNarrative
    assert "83" in stub.calls[0]["messages"][0]["content"]
