"""Shared helpers. The dashboard only reads the API; it has no database access of its own."""

from __future__ import annotations

import os

import httpx
import streamlit as st

API_URL = os.environ.get("API_URL", "http://localhost:8151")

# Categorical slots 1 and 2 of the reference palette (light), and its fixed status colors for tiers.
AUTONOMY_COLORS = {"No human touch": "#2a78d6", "Approved by a CSM": "#eb6834"}
TIER_COLORS = {"green": "#0ca30c", "yellow": "#fab219", "red": "#d03b3b"}
SINGLE = "#2a78d6"

ACTION_LABELS = {
    "create_renewal_deal": "Open renewal deal",
    "update_renewal_risk": "Update renewal risk",
    "open_recovery_task": "Recovery task",
    "create_expansion_task": "Expansion task",
    "escalate_to_csm": "Escalate to CSM",
    "send_checkin": "Check-in email",
    "send_onboarding_reminder": "Onboarding email",
    "send_ticket_followup": "Ticket follow-up email",
    "prepare_qbr": "QBR pack",
    "change_deal_amount": "Renewal amount change",
}


def get(path: str, **params):
    r = httpx.get(f"{API_URL}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def post(path: str, json: dict, token: str):
    return httpx.post(f"{API_URL}{path}", json=json, headers={"X-Approver-Token": token}, timeout=30)


def md(text: str) -> str:
    """Escape dollar signs so Streamlit's markdown doesn't read a pair of them as LaTeX."""
    return text.replace("$", "\\$")


def label(action_type: str) -> str:
    return ACTION_LABELS.get(action_type, action_type)


def header(title: str) -> dict:
    s = get("/metrics/summary")
    st.title(title)
    st.caption(f"Business date {s['today']} · synthetic portal · measurement window {s['window']['start']} to "
               f"{s['window']['end']} · data from {API_URL}")
    return s
