"""A deterministic stand-in for the model, so the stack, the seeded history and the tests run without an
API key. It reads cue phrases where Claude reads meaning, and it writes from templates. It also misstates
one QBR figure on about one first draft in eight, so the figure check has something real to catch."""

from __future__ import annotations

import hashlib

from .base import AccountReview, Reason, ToolCall

CUES = {
    "champion_left": ["leaving the practice", "is retiring", "new practice manager", "last day", "moving on"],
    "pricing": ["price increase", "too expensive", "cost per seat", "budget", "cheaper"],
    "competitor": ["another system", "a demo from", "evaluating", "switching to", "comparing"],
    "product_issue": ["not loading", "keeps crashing", "error when", "sync failed", "very slow"],
    "billing": ["invoice", "charged twice", "card was declined", "refund"],
}
CANCEL = ["cancel our", "not renewing", "terminate the contract", "end our contract", "cancel the subscription"]
REASON_TEXT = {
    "champion_left": "their main contact is leaving",
    "pricing": "they have raised price or budget",
    "competitor": "they are looking at another system",
    "product_issue": "they are hitting product problems",
    "billing": "there is a billing question open",
}


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


class OfflineLLM:
    provider = "offline"

    def review_account(self, context: dict, allowed_tools: list[str]) -> AccountReview:
        reasons, cancel, billing = [], False, False
        for item in context.get("recent_text", []):
            low = item["text"].lower()
            for code, cues in CUES.items():
                hit = next((c for c in cues if c in low), None)
                if hit and all(r.code != code for r in reasons):
                    reasons.append(Reason(code=code, evidence=_quote(item["text"], hit)))
            cancel = cancel or any(c in low for c in CANCEL)
            billing = billing or any(c in low for c in CUES["billing"])
        calls = []
        c, trigger = context["company"], context["trigger"]
        if "create_task" in allowed_tools:
            calls.append(ToolCall(name="create_task", arguments=_recovery_task(c, reasons)))
        if "send_checkin" in allowed_tools:
            if cancel and "escalate_to_csm" in allowed_tools:
                quote = next((_quote(i["text"], k) for i in context["recent_text"] for k in CANCEL if k in i["text"].lower()), "")
                calls.append(ToolCall(name="escalate_to_csm", arguments={
                    "summary": f"{c['name']} used cancellation language: \"{quote}\". No message was drafted."}))
            elif context.get("contacts"):
                calls.append(ToolCall(name="send_checkin", arguments=_checkin(context, trigger)))
        return AccountReview(reasons=reasons, cancellation_language=cancel, billing_language=billing, tool_calls=calls)

    def write_qbr(self, company: dict, figures: dict, attempt: int, feedback: list[str]) -> str:
        f = dict(figures)
        h = _h(company["id"], company.get("meeting_date"))
        # The stand-in's mistakes: a rounded utilization that drifts by a few points.
        if (attempt == 1 and h % 8 == 0) or (attempt == 2 and h % 8 == 0 and h % 7 == 0):
            f["seat_utilization_pct"] = f["seat_utilization_pct"] + 4
        trend = "up" if f["active_users_change_pct"] >= 0 else "down"
        return (
            f"Over the last quarter {company['name']} averaged {f['active_users_avg']} weekly active users on "
            f"{f['seats']} licensed seats, a seat utilization of {f['seat_utilization_pct']}%. Weekly active users are "
            f"{trend} {abs(f['active_users_change_pct'])}% against the quarter before.\n\n"
            f"The practice uses {f['modules_adopted']} of the 3 core modules. Your team opened {f['tickets_opened']} "
            f"support tickets in the last 90 days, {f['tickets_high']} of them high priority.\n\n"
            f"The contract renews on {f['renewal_date']}, {f['days_to_renewal']} days from now, at an annual value of "
            f"${f['arr']:,}."
        )


def _quote(text: str, cue: str) -> str:
    i = text.lower().find(cue)
    start = max(0, text.rfind(".", 0, i) + 1)
    end = text.find(".", i)
    return text[start:end if end != -1 else len(text)].strip()[:160]


def _recovery_task(c: dict, reasons: list[Reason]) -> dict:
    why = "; ".join(REASON_TEXT[r.code] for r in reasons) or "usage and engagement have dropped with no stated reason"
    weakest = min(c["points"], key=lambda k: c["points"][k] / max(c["weights"][k], 1))
    steps = {
        "seat_utilization": "Book a 20-minute call with the practice manager about which staff stopped logging in.",
        "module_adoption": "Offer a short session on the modules they have not switched on.",
        "ticket_ok": "Review the open tickets with support before the call so the fixes are in hand.",
        "invoice_current": "Check the billing status with finance before contacting the practice.",
    }[weakest]
    return {
        "subject": f"Health dropped to {c['tier']} ({c['score']}): {c['name']}",
        "body": f"Score {c['score']} ({c['tier']}). Signals: {why}. Weakest area: {weakest.replace('_', ' ')}. "
                f"First step: {steps} Renewal {c['renewal_date']}.",
        "due_in_days": 2 if c["tier"] == "red" else 5,
    }


def _checkin(ctx: dict, trigger: dict) -> dict:
    contact = ctx["contacts"][0]
    first = contact["name"].split()[0]
    csm = ctx.get("csm_name", "your customer success manager")
    kind = trigger["kind"]
    if kind == "onboarding":
        subject = "How the first weeks are going"
        body = (f"Hi {first},\n\nYou have been live for {trigger['days']} days. I can see the team is using "
                f"{ctx['figures']['modules_adopted']} of the 3 core modules so far. Would a 20-minute session on the rest "
                f"help? I have time this week.\n\n{csm}")
        sent_kind = "onboarding"
    elif kind == "ticket_followup":
        subject = "Following up on your support ticket"
        body = (f"Hi {first},\n\nI saw your ticket \"{trigger['subject']}\" was resolved. I wanted to check it is working "
                f"for the team now and that nothing else came out of it.\n\n{csm}")
        sent_kind = "ticket_followup"
    else:
        subject = "Checking in"
        if kind == "usage_drop":
            line = "I noticed fewer of your team logging in over the last couple of weeks"
        else:
            line = "It has been a while since we last spoke"
        body = (f"Hi {first},\n\n{line}, and I wanted to check whether anything is getting in the way. "
                f"Happy to set up a short call whenever suits you.\n\n{csm}")
        sent_kind = "checkin"
    return {"kind": sent_kind, "contact_id": contact["id"], "subject": subject, "body": body}
