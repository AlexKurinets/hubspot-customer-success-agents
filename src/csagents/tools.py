"""The typed tools the model works through.

`get_account_context` is a read and runs immediately. The write tools never touch HubSpot: a call is
validated against its schema and returned to the agent as a proposal, and the agent turns it into a
PlannedAction that goes through preconditions, the autonomy rules and the ledger like any other.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .llm.base import Reason


class GetAccountContext(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordReasons(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reasons: list[Reason] = Field(description="Reasons behind the account's current health, each with a quote")
    cancellation_language: bool = Field(description="True if the customer says they want to cancel or not renew")
    billing_language: bool = Field(description="True if the text raises an invoice, charge, refund or payment problem")


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(max_length=120)
    body: str = Field(description="A recovery plan for the CSM: what changed, what to do first, who to talk to")
    due_in_days: int = Field(ge=1, le=14)


class SendCheckin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["checkin", "onboarding", "ticket_followup"]
    contact_id: int
    subject: str = Field(max_length=120)
    body: str = Field(description="Plain, short email from the CSM to the customer. No prices, discounts or promises.")


class EscalateToCsm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(description="What the customer said and why a person should handle it")


TOOLS: dict[str, tuple[type[BaseModel], str]] = {
    "get_account_context": (GetAccountContext, "Read the account: health score and its points, renewal, usage figures, "
                                               "contacts and the recent ticket and email text."),
    "record_reasons": (RecordReasons, "Record the reasons behind the account's health and whether the customer used "
                                      "cancellation or billing language. Call this once."),
    "create_task": (CreateTask, "Propose a task for the account's CSM."),
    "send_checkin": (SendCheckin, "Propose an email from the CSM to one of the account's contacts."),
    "escalate_to_csm": (EscalateToCsm, "Hand the account to the CSM without drafting anything to the customer."),
}


def schema(name: str) -> dict:
    model, description = TOOLS[name]
    return {"name": name, "description": description, "input_schema": model.model_json_schema()}


def validate(name: str, arguments: dict) -> BaseModel:
    """Raises ValueError for unknown tools and ValidationError for bad arguments."""
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name}")
    return TOOLS[name][0].model_validate(arguments)


__all__ = ["TOOLS", "schema", "validate", "ValidationError"]
