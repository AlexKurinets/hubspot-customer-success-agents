"""The properties the agents own, created through the properties API so the CRM schema lives in version
control. Everything the agents may write freely starts with cs_agent_; any other field needs an
approved action (see executor.guard)."""

from __future__ import annotations

from .client import HubSpotClient

PREFIX = "cs_agent_"
GROUP = "cs_agent"
TIER_OPTIONS = [{"label": t.title(), "value": t, "displayOrder": i} for i, t in enumerate(["green", "yellow", "red"])]

AGENT_PROPERTIES = {
    "companies": [
        ("cs_agent_health_score", "Agent health score", "number", "number"),
        ("cs_agent_health_tier", "Agent health tier", "enumeration", "select"),
        ("cs_agent_health_reason", "Agent health reason", "string", "textarea"),
        ("cs_agent_reviewed_at", "Agent last review", "date", "date"),
    ],
    "deals": [
        ("cs_agent_risk_tier", "Agent renewal risk tier", "enumeration", "select"),
        ("cs_agent_action_key", "Agent action key", "string", "text"),
    ],
    "tasks": [("cs_agent_action_key", "Agent action key", "string", "text")],
    "notes": [("cs_agent_action_key", "Agent action key", "string", "text")],
    "emails": [("cs_agent_action_key", "Agent action key", "string", "text")],
}


def ensure_agent_properties(crm: HubSpotClient) -> list[str]:
    """Idempotent: creates whatever is missing and returns what it created."""
    created = []
    for object_type, props in AGENT_PROPERTIES.items():
        crm.ensure_group(object_type, GROUP, "Customer Success agents")
        for name, label, ptype, field_type in props:
            if not crm.property_exists(object_type, name):
                spec = {"name": name, "label": label, "type": ptype, "fieldType": field_type, "groupName": GROUP}
                if ptype == "enumeration":
                    spec["options"] = TIER_OPTIONS
                crm.create_property(object_type, spec)
                created.append(f"{object_type}.{name}")
    return created
