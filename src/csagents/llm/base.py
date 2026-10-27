"""What the agents ask of a model, as typed data. Both the Claude adapter and the offline stand-in return
these objects; nothing downstream knows which one ran."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

ReasonCode = Literal["champion_left", "pricing", "competitor", "product_issue", "billing"]


class LLMError(Exception):
    pass


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: ReasonCode
    evidence: str = Field(description="A short verbatim quote from the ticket or email text that shows the reason")


class ToolCall(BaseModel):
    name: str
    arguments: dict


class AccountReview(BaseModel):
    reasons: list[Reason] = []
    cancellation_language: bool = False
    billing_language: bool = False
    tool_calls: list[ToolCall] = []


class QbrNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(description="Three short paragraphs for the CSM to present, using only the figures given")


class LLM(Protocol):
    provider: str

    def review_account(self, context: dict, allowed_tools: list[str]) -> AccountReview: ...

    def write_qbr(self, company: dict, figures: dict, attempt: int, feedback: list[str]) -> str: ...
